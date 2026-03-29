FROM ghcr.io/atxinvox/frappe-microservice-lib:latest

WORKDIR /app
COPY . /app/service/
RUN touch /app/service/__init__.py

# Expose as Frappe app so the framework can load it
RUN ln -sf /app/service /app/sites/apps/expense_service

ENV SERVICE_PATH=/app/service
ENV SERVICE_APP=server:app
EXPOSE 8000
