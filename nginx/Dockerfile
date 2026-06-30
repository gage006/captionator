# --- build stage: compile the static frontend assets ---
# Build context is the repo root (see docker-compose.yml / docker-publish.yml),
# so frontend sources are copied from ./frontend.
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# --- prod stage: nginx serves the static build AND proxies /api to the backend ---
FROM nginx:alpine
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /app/dist /usr/share/nginx/html
