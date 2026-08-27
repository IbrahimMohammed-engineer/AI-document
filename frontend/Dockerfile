# ─── Build stage ──────────────────────────────────────────────────────────────
FROM node:20-alpine AS base
WORKDIR /app

# ─── Development stage ────────────────────────────────────────────────────────
FROM base AS development
# Copy package files
COPY package*.json ./
RUN npm install --legacy-peer-deps

# Copy source (in dev, this is overridden by the Docker Compose volume mount)
COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

# ─── Build stage ──────────────────────────────────────────────────────────────
FROM base AS builder
COPY package*.json ./
RUN npm ci --legacy-peer-deps
COPY . .
RUN npm run build

# ─── Production stage ─────────────────────────────────────────────────────────
FROM nginx:alpine AS production
COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
