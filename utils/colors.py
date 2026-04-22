"""
utils/colors.py
===============
Helper warna terminal untuk dashboard bot.
"""

def green(s):    return f"\033[92m{s}\033[0m"
def red(s):      return f"\033[91m{s}\033[0m"
def yellow(s):   return f"\033[93m{s}\033[0m"
def cyan(s):     return f"\033[96m{s}\033[0m"
def magenta(s):  return f"\033[95m{s}\033[0m"
def blue(s):     return f"\033[94m{s}\033[0m"
def bold(s):     return f"\033[1m{s}\033[0m"
def dim(s):      return f"\033[2m{s}\033[0m"
def white(s):    return f"\033[97m{s}\033[0m"

def bg_green(s):  return f"\033[42m{s}\033[0m"
def bg_red(s):    return f"\033[41m{s}\033[0m"

def clear_screen():
    print("\033[2J\033[H", end="")

def move_cursor(row, col):
    print(f"\033[{row};{col}H", end="")
