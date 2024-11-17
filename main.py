# --- Run te Redis Server and Celery Worker ---
# python -m celery -A main.celery_app worker --loglevel=info
# redis-server


import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import List, Optional

import aioboto3
import boto3
import botocore.exceptions
import dotenv
import magic
import uvicorn
from PIL import Image, UnidentifiedImageError
from PIL.Image import Resampling
from billiard.context import Process
from celery import Celery
from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from halo import Halo
from moviepy.editor import VideoFileClip
from redis.asyncio import Redis

# --- Load Environment Variables ---
dotenv.load_dotenv()

# --- Configurations ---
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "image-optimization-test-bucket-s3")
CLOUDFLARE_BASE_URL = os.getenv("CLOUDFLARE_BASE_URL", "https://your-cloudflare-cdn-url")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", 10 * 1024 * 1024))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "temp"))
RATE_LIMIT = int(os.getenv("RATE_LIMIT", 5))
RATE_LIMIT_WINDOW = int(os.getenv("RATE_LIMIT_WINDOW", 60))
USE_CLOUDFLARE = os.getenv("USE_CLOUDFLARE", "True").lower() in ["true", "1", "yes"]
redis_client: Redis = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


# --- Initialize Services ---
@asynccontextmanager
async def lifespan_context(fastapi_app: FastAPI):
    global redis_client
    redis_client = Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    await redis_client.ping()
    yield
    await redis_client.close()


app = FastAPI(
    title="Image Optimization API 🖼️",
    description="A simple API to upload, process, and optimize images.",
    version="1.0.0",
    docs_url="/",
    lifespan=lifespan_context
)

celery_app = Celery("tasks", broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0"))

# Update Celery configuration
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
)

# --- Setup Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- Setup CORS ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Create S3 Bucket ---
def create_s3_bucket(bucket_name: str):
    """Create an S3 bucket if it doesn't exist."""
    s3 = boto3.client('s3')
    region = os.getenv("AWS_REGION", "us-east-2")
    try:
        s3.head_bucket(Bucket=bucket_name)
        logger.info(f"S3 bucket '{bucket_name}' already exists.")
    except botocore.exceptions.ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == '403':
            logger.error(f"Access to S3 bucket '{bucket_name}' is forbidden. Check your permissions.")
        elif error_code == '404':
            try:
                if region == "us-east-1":
                    s3.create_bucket(Bucket=bucket_name)
                else:
                    s3.create_bucket(
                        Bucket=bucket_name,
                        CreateBucketConfiguration={'LocationConstraint': region}
                    )
                logger.info(f"S3 bucket '{bucket_name}' created successfully.")
            except botocore.exceptions.ClientError as create_error:
                if create_error.response['Error']['Code'] == 'BucketAlreadyExists':
                    logger.error(f"S3 bucket '{bucket_name}' already exists globally. Choose a different name.")
                else:
                    logger.error(f"Failed to create S3 bucket '{bucket_name}': {create_error}")
        else:
            logger.error(f"Failed to access S3 bucket '{bucket_name}': {e}")


# Create the S3 bucket if it doesn't exist
create_s3_bucket(S3_BUCKET_NAME)


# --- Rate Limiting Decorator ---
def rate_limit(limit: int, window: int):
    def decorator(func):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            client_ip = request.client.host
            key = f"rate_limit:{client_ip}"
            current_count = await redis_client.get(key)

            if current_count and int(current_count) >= limit:
                logger.warning(f"Rate limit exceeded for IP: {client_ip}")
                raise HTTPException(status_code=429, detail="Rate limit exceeded 🚫")

            if not current_count:
                await redis_client.set(key, 1, ex=window)
            else:
                await redis_client.incr(key)

            return await func(request, *args, **kwargs)

        return wrapper

    return decorator


# --- Helper Functions ---
def handle_exception(e: Exception, message: str):
    """Handle and log exceptions."""
    logger.error(f"{message}: {e} ❌")
    raise HTTPException(status_code=500, detail="Internal Server Error 💥")


def detect_file_type(file_path: str) -> str:
    """Detect the file type using python-magic."""
    mime = magic.Magic(mime=True)
    return mime.from_file(file_path)


def save_to_local(file: UploadFile) -> str:
    """Save uploaded file locally with size validation and file type detection."""
    try:
        if file.size > MAX_FILE_SIZE:
            logger.warning(f"File size exceeds limit: {file.filename}")
            raise HTTPException(status_code=400, detail="File size exceeds 10 MB limit 🚫")

        file_id = str(uuid.uuid4())
        local_path = TEMP_DIR / f"{file_id}_{file.filename}"
        local_path.parent.mkdir(parents=True, exist_ok=True)

        with local_path.open("wb") as f:
            f.write(file.file.read())

        detected_type = detect_file_type(str(local_path))
        logger.info(f"Detected file type: {detected_type}")

        if detected_type not in {"image/jpeg", "image/png", "image/gif"}:
            logger.warning(f"Unsupported file type: {detected_type}")
            raise HTTPException(status_code=400, detail="Unsupported file type 🚫")

        logger.info(f"File saved locally: {local_path}")
        return str(local_path)
    except Exception as e:
        handle_exception(e, "Error saving file locally")


async def async_upload_to_s3(local_path: str, s3_key: str) -> str:
    """Upload a file asynchronously to S3."""
    try:
        session = aioboto3.Session()
        async with session.client("s3") as client:
            await client.upload_file(local_path, S3_BUCKET_NAME, s3_key)
        logger.info(f"File uploaded to S3: {s3_key}")
        return f"{CLOUDFLARE_BASE_URL}/{s3_key}" if USE_CLOUDFLARE else f"https://s3.amazonaws.com/{S3_BUCKET_NAME}/{s3_key}"
    except Exception as e:
        handle_exception(e, "Error uploading file to S3")


async def cache_image(image_id: str, cdn_url: str):
    """Cache the CDN URL in Redis."""
    try:
        await redis_client.set(image_id, cdn_url)
        logger.info(f"Image cached in Redis: {image_id}")
    except Exception as e:
        handle_exception(e, "Error caching image URL")


async def get_cached_image(image_id: str) -> Optional[str]:
    """Retrieve cached CDN URL from Redis."""
    try:
        cached_url = await redis_client.get(image_id)
        logger.info(f"Retrieved cached image URL: {image_id}")
        return cached_url
    except Exception as e:
        handle_exception(e, "Error retrieving cached image URL")


def optimize_image(local_path: str, width: int = 800, height: int = 600, quality: int = 85) -> str:
    """Optimize image with custom dimensions and quality."""
    try:
        if not Path(local_path).exists():
            raise FileNotFoundError(f"File not found: {local_path}")

        output_path = local_path.replace(".jpg", "_optimized.jpg").replace(".png", "_optimized.png").replace(".jpeg",
                                                                                                             "_optimized.jpeg").replace(
            ".gif", "_optimized.gif")
        with Image.open(local_path) as img:
            img = img.resize((width, height), Resampling.LANCZOS)
            img.save(output_path, optimize=True, quality=quality)
        return output_path
    except (FileNotFoundError, UnidentifiedImageError, OSError) as e:
        logger.error(f"Error optimizing image: {e}")
        raise
    except Exception as e:
        handle_exception(e, "Error optimizing image")


def gif_to_mp4(local_path: str) -> str:
    """Convert GIF to MP4 using MoviePy."""
    try:
        output_path = local_path.replace(".gif", ".mp4")
        clip = VideoFileClip(local_path)
        clip.write_videofile(output_path, codec="libx264", audio=False)
        logger.info(f"GIF converted to MP4: {output_path}")
        return output_path
    except Exception as e:
        handle_exception(e, "Error converting GIF to MP4")


@celery_app.task(bind=True, max_retries=3)
def process_file(self, file_path: str, file_type: str, file_id: str, width: int = 800, height: int = 600,
                 quality: int = 85):
    """Process file."""
    spinner = Halo(text='Processing file', spinner='dots')

    async def run_async_tasks():
        optimized_path = None
        try:
            await redis_client.set(f"progress:{file_id}", "Processing started 🛠️")
            progress_steps = 3
            step_size = 100 / progress_steps
            current_progress = 0

            if file_type in {"image/jpeg", "image/png", "image/gif"}:
                await redis_client.set(f"progress:{file_id}", f"Processing: {current_progress:.0f}%")
                current_progress += step_size
                if file_type in {"image/jpeg", "image/png"}:
                    await redis_client.set(f"progress:{file_id}", f"Optimizing image: {current_progress:.0f}%")
                    optimized_path = optimize_image(file_path, width, height, quality)
                elif file_type == "image/gif":
                    await redis_client.set(f"progress:{file_id}", f"Converting GIF to MP4: {current_progress:.0f}%")
                    optimized_path = gif_to_mp4(file_path)

            else:
                raise ValueError("Unsupported file type")

            await redis_client.set(f"progress:{file_id}", f"Uploading to S3: {current_progress:.0f}%")
            current_progress += step_size
            s3_key = f"optimized/{Path(optimized_path).name}"
            cdn_url = await async_upload_to_s3(optimized_path, s3_key)
            await cache_image(file_id, cdn_url)

            if Path(file_path).exists():
                Path(file_path).unlink()
            if Path(optimized_path).exists():
                Path(optimized_path).unlink()

            await redis_client.set(f"progress:{file_id}", "Completed ✅")
            spinner.succeed("File processed successfully")
            logger.info(f"File processing completed: {file_id}")
        except FileNotFoundError as e:
            await redis_client.set(f"progress:{file_id}", "Failed ❌")
            spinner.fail("File processing failed")
            logger.error(f"File not found: {e}")
        except Exception as e:
            await redis_client.set(f"progress:{file_id}", "Failed ❌")
            spinner.fail("File processing failed")
            self.retry(exc=e)
            handle_exception(e, "Error processing file")

    def run_in_process():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_async_tasks())
        loop.close()

    process = Process(target=run_in_process)
    process.start()
    process.join()


# --- API Endpoints ---
@app.post("/v1/upload-image", tags=["📤 upload"], summary="Upload an image", description="Upload and process an image.")
@rate_limit(RATE_LIMIT, RATE_LIMIT_WINDOW)
async def upload_image(request: Request, file: UploadFile = File(...), width: int = 800, height: int = 600,
                       quality: int = 85, background_tasks: BackgroundTasks = BackgroundTasks()):
    """Handle image uploads."""
    try:
        file_id = str(uuid.uuid4())
        cached_url = await get_cached_image(file_id)

        if cached_url:
            return JSONResponse({"cdn_url": cached_url})

        file_path = save_to_local(file)

        background_tasks.add_task(process_file.delay, file_path, file.content_type, file_id, width, height, quality)

        return JSONResponse({"message": "File is being processed 🛠️", "file_id": file_id})
    except Exception as e:
        handle_exception(e, "Error uploading image")


@app.post("/v1/bulk-upload", tags=["📤 upload"], summary="Bulk upload images",
          description="Upload multiple images simultaneously.")
@rate_limit(RATE_LIMIT, RATE_LIMIT_WINDOW)
async def bulk_upload(request: Request, files: List[UploadFile] = File(...), width: int = Query(800),
                      height: int = Query(600), quality: int = Query(85),
                      background_tasks: BackgroundTasks = BackgroundTasks()):
    try:
        file_ids = []
        for file in files:
            file_id = str(uuid.uuid4())
            file_path = save_to_local(file)
            background_tasks.add_task(process_file.delay, file_path, file.content_type, file_id, width, height, quality)
            file_ids.append(file_id)
        return JSONResponse({"message": "Files are being processed 🛠️", "file_ids": file_ids})
    except Exception as e:
        handle_exception(e, "Error during bulk upload")


@app.get("/v1/image-status/{file_id}", tags=["📊 status"], summary="Check image processing status.",
         description="Check whether an image has been processed and uploaded.")
@rate_limit(RATE_LIMIT, RATE_LIMIT_WINDOW)
async def check_status(request: Request, file_id: str):
    """Check if the image is processed and cached."""
    try:
        cached_url = await get_cached_image(file_id)
        progress = await redis_client.get(f"progress:{file_id}")

        if cached_url:
            logger.info(f"Image processing status: {file_id} - Completed")
            return JSONResponse({"cdn_url": cached_url, "progress": progress})
        logger.info(f"Image processing status: {file_id} - In Progress")
        return JSONResponse({"message": "Processing not complete yet ⏳", "progress": progress}, status_code=202)
    except Exception as e:
        handle_exception(e, "Error checking status")


@app.get("/v1/health", tags=["🏥 health"], summary="Check API health", description="Check the health status of the API.")
def health_check():
    """Check the health status of the API."""
    logger.info("Health check endpoint called")
    return JSONResponse({"message": "API is healthy 🏥"})


# --- Run the API ---
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
