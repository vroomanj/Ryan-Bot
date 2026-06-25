# Ryan-Bot Project Rules

- **Channel Matching**: Almost all channels in the target Discord server have an emoji and some other text before their name. When writing code to search for channels, ALWAYS use fuzzy/substring matching (e.g., `"general" in channel.name.lower()`) rather than exact matching.
