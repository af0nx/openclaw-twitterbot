# Repository Boundaries

OpenClaw is the public AI assistant, gateway, app, plugin, skill, and channel-integration product. Keep this repository focused on OpenClaw source, docs, tests, release assets, and product operations.

SkinBetHub/Twitter bot code, schemas, PM2 configs, bot Docker files, generated media, database dumps, model artifacts, and bot-specific deployment docs belong in the private `skinbethub_twitter` repository. They should not be introduced into OpenClaw except as explicitly documented external integration examples.

Before adding new operational content, verify that the file supports OpenClaw itself. If it belongs to another product, place it in that product repository and link to it only when the OpenClaw user needs the reference.
