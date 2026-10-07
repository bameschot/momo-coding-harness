"""Small helpers."""


def parse_duration(text):
    """Minutes in a duration like '1h30m', '2h' or '45m'."""
    text = text.strip().lower()
    minutes = 0
    number = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch.isdigit():
            number = number + ch
        elif ch == "h":
            if number == "":
                number = "0"
            minutes = minutes + int(number) * 60
            number = ""
        elif ch == "m":
            if number == "":
                number = "0"
            minutes = minutes + int(number)
            number = ""
        elif ch == " ":
            pass
        else:
            return None
        i = i + 1
    if number != "":
        minutes = minutes + int(number)
    return minutes


def slug(text):
    return "-".join(text.lower().split())
