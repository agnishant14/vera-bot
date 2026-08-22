# Vera Signal Engine

FastAPI service for the magicpin merchant-assistant challenge. It provides a deterministic `compose(...)` function and the HTTP API required for deployment.

## Run locally

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn bot:app --host 0.0.0.0 --port 8080
```

Check that it is running:

```bash
curl http://localhost:8080/v1/healthz
```

Interactive API documentation is available at `http://localhost:8080/docs`.

To load the expanded local dataset and run a sample tick:

```bash
python3 load_contexts.py --tick
```

## API

- `GET /v1/healthz`
- `GET /v1/metadata`
- `POST /v1/context`
- `POST /v1/tick`
- `POST /v1/reply`
- `POST /v1/teardown`

JSON `POST` requests must include `Content-Type: application/json`.

## Deploy on Render

The repository includes `render.yaml` and a Dockerfile.

1. Push the repository to GitHub.
2. In Render, choose **New -> Blueprint** and select the repository.
3. Set `TEAM_NAME`, `TEAM_MEMBERS`, `CONTACT_EMAIL`, and `SUBMITTED_AT`.
4. Deploy and wait for the service to become **Live**.
5. Verify the generated URL:

```bash
curl https://YOUR-SERVICE.onrender.com/v1/healthz
curl https://YOUR-SERVICE.onrender.com/v1/metadata
```

Submit the base URL, for example:

```text
https://YOUR-SERVICE.onrender.com
```

The evaluator loads its own contexts through the API. Do not pre-load the expanded dataset on the deployed service.

## Generate submission

```bash
python3 generate_submission.py --expanded-dir /path/to/magicpin-ai-challenge/expanded
```
