FROM python:3.12-alpine

# Set build arguments
ARG RELEASE_VERSION
ENV RELEASE_VERSION=${RELEASE_VERSION}

# Install ffmpeg, su-exec, deno and Chromaprint (fpcalc, for audio verification)
RUN apk update && apk add --no-cache ffmpeg su-exec deno chromaprint

# Create directories and set permissions
COPY . /lidatube
WORKDIR /lidatube

# Install requirements
ENV PYTHONPATH=/lidatube/src
RUN pip install --no-cache-dir -r requirements.txt

# Make the script executable
RUN chmod +x init.sh

# Expose port
EXPOSE 5000

# Start the app
ENTRYPOINT ["./init.sh"]

