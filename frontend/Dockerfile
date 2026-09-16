# NOTE: not built on the development machine -- Docker is not installed there. It IS built and
# run on every push: the `docker-runtime` job in .github/workflows/ci.yml builds this image and
# drives the running container, asserting that nginx serves the built SPA at /, /index.html and
# the client-side routes, that /api/ is proxied to the API rather than swallowed by the fallback,
# and that no build-time secret reaches the served assets.
#
# Node 24 because that is the only version this project actually verifies: CI runs
# actions/setup-node with node-version 24, and the developer machine runs 24.21.0. Vite 6
# supports 20 as well, so the previous node:20-alpine was not broken -- it was simply a
# version nothing in this repository ever tested, which is a poor thing to ship from.
FROM node:24-alpine AS build

WORKDIR /app

# The lockfile belongs in THIS layer, beside package.json.
#
# Previously only package.json was copied here and the install was `npm install`, so the
# lockfile -- which arrived later, with the source -- had no effect at all: the image resolved
# fresh versions from the "^" ranges and could ship dependencies CI had never seen. `npm ci`
# installs exactly what package-lock.json records and fails outright if the two disagree,
# which is what makes this build reproducible. It is also what CI already does.
#
# No `--omit=dev`: vite and typescript are devDependencies, and this stage is the build.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Build-time, not runtime: Vite inlines this at compile time, so changing the API origin means
# rebuilding the image. Empty means "same origin as the page", which is what the compose
# topology uses -- the nginx stage below proxies /api to the api service, so the browser never
# needs a second origin and no CORS is involved.
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build

FROM nginx:1.27-alpine AS runtime

# Replaces the stock server block rather than sitting beside it: the image's own default.conf
# is what would otherwise serve this directory with no SPA fallback, no cache policy and no
# security headers.
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Only the built output crosses the stage boundary. No Node, no npm, no node_modules, no
# source tree, no tests -- the runtime image is nginx plus static files.
COPY --from=build /app/dist /usr/share/nginx/html

# 443 is the application; 80 exists only to redirect to it (and to answer the container
# healthcheck at /healthz). No certificate is copied here on purpose -- it is mounted at
# /etc/nginx/certs by docker-compose.yml, so this image carries no deployment secret and is
# identical in every environment.
EXPOSE 80 443
CMD ["nginx", "-g", "daemon off;"]
