# Use official Python image
FROM python:3.11-slim

WORKDIR /app

# Install system deps for building packages (if necessary)
RUN apt-get update && apt-get install -y build-essential --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the project into the image so templates and static assets are available
COPY . /app

# Expose default uvicorn port
EXPOSE 8022

# Run the app using the module path so the app in `app/main.py` is found
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8022"]
