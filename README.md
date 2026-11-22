```powershell
docker compose up -d --wait postgres
python -m venv .venv
cd backend
..\.venv\Scripts\python.exe -m pip install -c requirements-lock.txt -e .
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m miniharness.main
```

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```
