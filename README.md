# Image Optimization API 🖼️

A FastAPI-based service to upload, process, and optimize images with features like rate limiting, background task
processing, and cloud storage integration using AWS S3. This API is perfect for developers looking to streamline their
image processing pipeline.

---

## Features 🚀

1. **Image Optimization**  
   Resize and optimize images (JPEG, PNG, GIF) with custom dimensions and quality settings.

2. **GIF to MP4 Conversion**  
   Automatically convert GIF files to MP4 format for better performance.

3. **Rate Limiting**  
   Prevent abuse with customizable IP-based rate limiting.

4. **Asynchronous Processing**  
   Efficiently process uploads using Celery workers and Redis.

5. **Cloud Integration**  
   Upload optimized files to AWS S3 and optionally deliver via Cloudflare CDN.

6. **Progress Tracking**  
   Monitor image processing status in real-time via Redis.

7. **Bulk Uploads**  
   Handle multiple files in a single API call.

8. **Health Check Endpoint**  
   Verify the API's health status at any time.

---

## Prerequisites 🛠️

1. **Environment Variables**  
   Create a `.env` file with the following variables:
   ```env
   AWS_ACCESS_KEY_ID=<your-access-key-id>
   AWS_SECRET_ACCESS_KEY=<your-secret-access-key>
   AWS_REGION=us-east-2
   S3_BUCKET_NAME=image-optimization-test-bucket-s3
   CLOUDFLARE_BASE_URL=https://your-cloudflare-cdn-url
   REDIS_HOST=localhost
   REDIS_PORT=6379
   CELERY_BROKER_URL=redis://localhost:6379/0
   MAX_FILE_SIZE=10485760  # 10 MB
   TEMP_DIR=temp
   RATE_LIMIT=5
   RATE_LIMIT_WINDOW=60  # 60 seconds
   USE_CLOUDFLARE=True
    ```
   Replace the placeholders with your own values.

2. **Dependencies**  
   Install the required Python packages using:
   ```bash
   pip install -r requirements.txt
   ```

3. **Redis Server**  
   Start a Redis server using Docker:
   ```bash
   redis-server

   ```

4. **Celery Worker**
   Run the Celery worker in a separate terminal window:
   ```bash
   python -m celery -A main.celery_app worker --loglevel=info
   ```

5. **FastAPI Server**
   Run the FastAPI server in another terminal window:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8000

   ```

6. **API Documentation**
   Open the API documentation in your browser:
      ```
      http:// localhost:8000/docs
      ```

---

## Usage 📝

1. **Upload an Image**  
   Send a `POST` request to `/upload` with the image file as a form-data parameter:

    ```bash
    curl -X POST "http://localhost:8000/v1/upload-image" \
   -H "accept: application/json" \
   -H "Content-Type: multipart/form-data" \
   -F "file=@example.jpg"
    ```
 
   
   

## Tech Stack 🛠️

- **FastAPI**  
  A modern, fast (high-performance), web framework for building APIs with Python 3.6+ based on standard Python type hints.

- **Celery**
    A distributed task queue that handles background processing, such as image optimization, in a separate worker process.

- **Redis**
    An in-memory data structure store used as a broker for Celery tasks and for real-time progress tracking.

- **AWS S3**
    A cloud storage service for storing and retrieving optimized images.

- **Cloudflare**
    A content delivery network (CDN) for caching and delivering images to users around the world.


