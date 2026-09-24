# Telegram AI Bot — Railway

## Railway Variables

Set these variables in Railway:

BOT_TOKEN=8458055842:AAEDoEfD9cBpxTZNDXp7sZlxpgIdFMCFyOU
OPENAI_API_KEY=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
ADMIN_IDS=7575502917
BOT_USERNAME=@NOORI_HOSHA_BOT
DATABASE_URL=postgresql+asyncpg://...   # optional if Railway PostgreSQL is attached

Optional:
OPENAI_TEXT_MODEL=gpt-5.6-luna
OPENAI_IMAGE_MODEL=gpt-image-2

If DATABASE_URL is omitted, the bot uses SQLite. For Railway production, attach PostgreSQL
and use the PostgreSQL connection URL so user data survives deployments.

## Deploy

Upload:
- bot.py
- requirements.txt
- Procfile

Railway should install requirements and run:
python bot.py

## Important

1. Never paste your OpenAI API key into Telegram or public source code.
2. The old API key that was exposed in the chat should be revoked and replaced.
3. Add the bot as an administrator in channels used for mandatory membership checks.
4. For groups, add the bot to the group and then run:
   /enablegroup GROUP_CHAT_ID
5. To add mandatory channel:
   /addchannel CHAT_ID | Channel Name | https://t.me/your_invite_link

## Admin commands

/admin
/addadmin USER_ID
/deladmin USER_ID
/ban USER_ID
/unban USER_ID
/addchannel CHAT_ID | TITLE | INVITE_LINK
/delchannel ID
/enablegroup CHAT_ID
/disablegroup CHAT_ID

## Features included

- Persian/Dari and English language selection
- AI chat using OpenAI Responses API
- Text-to-image
- Referral links, +1 point per successful new referral
- 3 free minutes per rolling hour
- 3 points per paid 5-minute block
- User information and referral link
- Like/dislike buttons
- 48-hour conversation cleanup
- Support inbox sent to admins; admin replies with Telegram Reply
- Broadcast
- Admin management
- User ban/unban
- Mandatory channel membership
- Group AI when mentioned
- Group enable/disable
- Statistics
