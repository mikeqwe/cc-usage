FROM python:3.12-slim

WORKDIR /app
COPY server.py index.html rates.json ./
COPY vendor ./vendor
RUN mkdir -p data

EXPOSE 8765

# Mount ~/.claude/projects as /claude-projects at runtime
ENV CLAUDE_PROJECTS_DIR=/claude-projects

CMD ["python3", "server.py", "--no-open"]
