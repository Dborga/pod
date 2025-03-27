#!/bin/sh
echo "PORT is: $PORT"
exec gunicorn --bind "0.0.0.0:${PORT:-5000}" app:app
