FROM node:20-slim

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY poller.js ./

# Cloud Run Jobs just run the container to completion — no server, no PORT needed.
CMD ["node", "poller.js"]
