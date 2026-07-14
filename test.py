import sys

print(sys.version)
try:
    raise ValueError("test")
except TypeError, ValueError:
    print("caught")
