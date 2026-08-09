# Clipper Handz Demo Backend

FastAPI backend powered by the OpenAI Agents SDK. It contains three stateful agents:

- AI receptionist for questions, availability, and bookings
- Speed-to-Lead confirmation agent for appointment changes and cancellation
- Review follow-up agent for feedback and customer-issue routing

SQLite stores runtime demo records and each agent's conversation memory.

## Deploy on Render

### Blueprint deployment

1. Create a new Render Blueprint from this repository.
2. Render reads `render.yaml` and builds the included `Dockerfile`.
3. Add these environment variables in Render:

   - `OPENAI_API_KEY` — required and secret
   - `OPENAI_MODEL` — optional; defaults to `gpt-4.1-mini`
   - `CORS_ORIGINS` — comma-separated frontend origins, without trailing slashes

   Example:

   ```text
   https://clipperhandz-ai-receptionist-demo.vercel.app,https://your-custom-domain.com
   ```

4. Use `/health` as the health-check path.
5. After deployment, set the frontend's `VITE_API_BASE_URL` to the Render service URL and redeploy the frontend.

### Manual Render web service

- Runtime: **Docker**
- Dockerfile path: `./Dockerfile`
- Health check: `/health`
- Required secret: `OPENAI_API_KEY`

## Persistence

The demo works without a persistent disk, but SQLite data resets whenever Render replaces the container. To retain sessions and appointments across deploys, attach a Render persistent disk at:

```text
/app/backend/db
```

Keep the service at one instance when using SQLite.

## Local Docker test

From this repository root:

```bash
docker build -t clipperhandz-demo-backend .
docker run --rm -p 8000:8000 \
  -e OPENAI_API_KEY=your-key \
  -e CORS_ORIGINS=http://localhost:4173 \
  clipperhandz-demo-backend
```

Then open `http://localhost:8000/health` or `http://localhost:8000/docs`.

Never commit `.env`, OpenAI API keys, or SQLite database files.
