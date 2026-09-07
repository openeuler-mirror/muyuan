import json, subprocess; value = int(subprocess.check_output(["python", "guess.py"], text=True).strip()); print(json.dumps({"guess_score": 1 - abs(1 - value)}))
