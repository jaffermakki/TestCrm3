CATEGORY_LABELS = {
    "CASE": "📱 Case", "CHARGER": "🔌 Charger", "CABLE": "🔗 Cable",
    "SCREEN": "🖥️ Screen", "BATTERY": "🔋 Battery", "ACCESSORY": "🎧 Accessory", "PART": "🔧 Part",
}
CAT_SUBCATEGORIES = {
    "CASE": ["OtterBox", "Speck", "UAG", "Moment", "Case-Mate", "RhinoShield", "Peel", "Casetify", "Mous", "Nomad", "Apple", "Generic"],
    "CHARGER": ["Anker", "Apple", "Belkin", "Samsung", "RAVPower", "Generic"],
    "CABLE": ["Anker", "Apple", "Belkin", "Generic"],
    "SCREEN": ["OEM", "Aftermarket", "Glass-Only"],
    "BATTERY": ["OEM Grade", "Aftermarket"],
    "ACCESSORY": ["Screen Protector", "Wireless Charging", "PopSocket", "Holder", "Other"],
    "PART": ["General"],
}
ALL_BRANDS = sorted(set(b for subs in CAT_SUBCATEGORIES.values() for b in subs))
