"""Centralized Playwright CSS / XPath selectors for Google Flow (VideoFX)."""

# URLs
FLOW_URL = "https://aitestkitchen.withgoogle.com/tools/video-fx"
GOOGLE_LOGIN_URL = "https://accounts.google.com"

# Selectors (Subject to Google UI revisions)
PROMPT_TEXTAREA = "textarea"
GENERATE_BUTTON = "button:has-text('Generate')"
DOWNLOAD_BUTTON = "button[aria-label='Download']"

# Aspect ratio controls (9:16 vertical short-form)
ASPECT_RATIO_SELECTOR = "button[aria-label*='Aspect Ratio'], button:has-text('Aspect Ratio')"
ASPECT_RATIO_9_16_OPTION = "button:has-text('9:16'), [data-value='9:16'], [aria-label='9:16']"

# Ingredient / Reference image upload
INGREDIENT_FILE_INPUT = "input[type='file']"
