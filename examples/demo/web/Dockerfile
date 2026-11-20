FROM busybox:1.36
# The build context is web/, so `.` here is web/, not the project root.
COPY . /www/
CMD ["httpd", "-f", "-v", "-p", "8080", "-h", "/www"]
