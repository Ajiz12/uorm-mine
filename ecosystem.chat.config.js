module.exports = {
  apps: [
    {
      name: "uorm-chat",
      script: "uorm_chat_bot.py",
      interpreter: "python3",
      cwd: "uorm-mine",
      autorestart: true,
      max_restarts: 50,
      restart_delay: 10000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
      error_file: "uorm-mine/logs/uorm-chat-error.log",
      out_file: "uorm-mine/logs/uorm-chat-out.log",
      merge_logs: true,
      time: true,
    },
  ],
};
