web: python scripts/migrate.py && python -m uvicorn coinfinder.api.main:app --host 0.0.0.0 --port ${PORT:-8000}
worker: python -m coinfinder.worker.main
bot: python -m coinfinder.bot.main
