# Sample image: nested under the project, so discovery names it
# <project>-<subdir> -> `dashboard-worker`.
FROM node:22-alpine

ARG APK_REFRESH=unset
RUN echo "apk refresh: $APK_REFRESH" && apk --no-cache upgrade

WORKDIR /app
COPY package.json ./
COPY server.js ./
CMD ["node", "server.js"]
