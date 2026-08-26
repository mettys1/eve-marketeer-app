FROM node:20-slim

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY lib.js job_my_orders.js job_perimeter.js job_wallet.js ./

# One image, two Cloud Run Jobs — each job's `gcloud run jobs create` overrides the
# container command/args to pick job_my_orders.js or job_perimeter.js (see
# deploy_esi.sh). This default is just so `docker run` alone does something sane.
CMD ["node", "job_my_orders.js"]
