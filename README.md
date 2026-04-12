# Telegram Assistant Bot

A modern Telegram assistant bot with AI chat, quick weather access for selected cities, and a smooth button-driven interface.

## Features

- Multi-AI support
- Button-based navigation
- Quick weather checks
- Clean English bot UI
- Railway deployment
- Public-repo-safe setup with environment variables

## Weather cities

- Sofia
- Pernik
- Amsterdam
- Larnaca

## Tech stack

- Python
- python-telegram-bot
- Google Gemini API
- Groq API
- OpenRouter
- Open-Meteo
- Railway

## Environment variables

Set these in Railway:

- `TELEGRAM_TOKEN`
- `GEMINI_KEY`
- `GROQ_KEY`
- `OPENROUTER_KEY`

## Notes

Do not commit real API keys or tokens to the repository.
Use environment variables for all secrets.