PROVINCE_TAX = {
    "ON": [("HST", 0.13)],
    "BC": [("GST", 0.05), ("PST", 0.07)],
    "AB": [("GST", 0.05)],
    "SK": [("GST", 0.05), ("PST", 0.06)],
    "MB": [("GST", 0.05), ("PST", 0.07)],
    "QC": [("GST", 0.05), ("QST", 0.09975)],
    "NB": [("HST", 0.15)],
    "NS": [("HST", 0.15)],
    "PE": [("HST", 0.15)],
    "NL": [("HST", 0.15)],
}


def calc_canadian_tax(taxable: float, province: str = "ON"):
    rates = PROVINCE_TAX.get(province, PROVINCE_TAX["ON"])
    lines = []
    tax_total = 0.0
    for label, rate in rates:
        amount = round(taxable * rate, 2)
        lines.append({"label": f"{label} ({rate * 100:.3g}%)", "amount": amount})
        tax_total += amount
    tax_total = round(tax_total, 2)
    return {
        "lines": lines,
        "tax_total": tax_total,
        "total": round(taxable + tax_total, 2),
    }

PROVINCE_LABELS = {
    "ON": "Ontario — HST 13%", "BC": "British Columbia — GST+PST 12%",
    "AB": "Alberta — GST 5%", "SK": "Saskatchewan — GST+PST 11%",
    "MB": "Manitoba — GST+PST 12%", "QC": "Quebec — GST+QST 14.975%",
    "NB": "New Brunswick — HST 15%", "NS": "Nova Scotia — HST 15%",
    "PE": "Prince Edward Island — HST 15%", "NL": "Newfoundland — HST 15%",
}
