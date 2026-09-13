# Sample image: one Dockerfile at the project root, so discovery names
# the image after the project itself -> `docs-site`.
FROM nginx:alpine

# build-images.sh passes a dated APK_REFRESH/APT_REFRESH to every build.
# Referencing it here is what makes the upgrade layer's cache key move
# once a day, so a rebuild actually picks up base-OS security fixes
# instead of restoring a byte-identical cached layer.
ARG APK_REFRESH=unset
RUN echo "apk refresh: $APK_REFRESH" && apk --no-cache upgrade

COPY index.html /usr/share/nginx/html/index.html
EXPOSE 80
