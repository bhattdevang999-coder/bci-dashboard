#!/bin/bash
mkdir -p uploads/templates uploads/products uploads/keywords uploads/output feedback brand_configs
exec gunicorn app:app --bind 0.0.0.0:$PORT --timeout 300 --workers 2
