import pickle

with open("data-2026-01-20-PAPER.pickle", "rb") as f:
    data = pickle.load(f)

print(type(data))
if isinstance(data, dict):
    print("Keys:", data.keys())
elif isinstance(data, list):
    print("First element type:", type(data[0]))
    print("Sample:", data[0])
else:
    print("Data:", data)