"""Shared product taxonomy for image and text classification."""

CATEGORY_RULES = """
Classify the actual product being sold by its main use, not the seller, brand,
advertising, compatible device, or a word fragment. Use these exact categories:
CLOTHING: clothes, shoes, bags and fashion accessories (니트, 청바지, 운동화, 가방).
FOOD: food, groceries, snacks and drinks (우유, 쌀, 과자, 라면, 커피 원두).
DIGITAL: phones/computers, audio, cameras and their accessories, including tablet
screen protectors, phone cases, keyboards and chargers.
APPLIANCES: powered household appliances, e.g. vacuums, fans, coffee machines, hair dryers.
BEAUTY: makeup, skincare, cosmetics and cleansing products (립스틱, 클렌징 패드).
FURNITURE: furniture and interior decor (책상, 소파, 커튼, 소파 쿠션).
HOBBY: sports, camping, arts, books, collectibles and hobby equipment (요가매트, 캠핑 의자).
GAME: video/board games, consoles and gaming-specific accessories.
DAILY_LIVING: cleaning, hygiene, kitchen and everyday household supplies (치약, 세제, 컵).
OTHER: only when the product cannot be identified or none of the above fits.
Do not default recognizable clothing or food to OTHER. A makeup cushion is BEAUTY,
a sofa cushion is FURNITURE; coffee beans are FOOD, a coffee machine is APPLIANCES.
"""
