ANNIEMUSIC YouTube API - Render package (proxy removed)

Files: app.py, Dockerfile, requirements.txt, render.yaml

Endpoints:
GET /health
GET /info/{video_id}
GET /info/bulk?video_ids=ID1,ID2
GET /download?url=YOUTUBE_URL&type=audio
GET /download?url=YOUTUBE_URL&type=video
GET /stream/{video_id}?type=audio&token=TOKEN
GET /stream/{video_id}?type=video&token=TOKEN
GET /clear_cache

Flow:
1. Request /download?url=YOUTUBE_URL&type=audio (or video)
2. Copy video_id and token from JSON response
3. Request /stream/VIDEO_ID?type=audio&token=TOKEN

Token expires after 20 minutes. Tokens are held in process memory, so keep one Render instance/worker.
Cookies are optional; without cookies YouTube can still challenge/block requests.
Normal Render web-service disk is ephemeral, so cache can disappear on restart/redeploy.
No proxy URLs or proxy credentials remain in this project.

Deploy:
Use a Render Web Service from a Git repo containing these files, or build/push
the Dockerfile image to a container registry and deploy that image. Render
does not run a raw ZIP upload directly as a FastAPI web service.
