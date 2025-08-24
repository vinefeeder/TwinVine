import subprocess
import os
import threading


def _batchloader():
    try:
        with open('./batch.txt', 'r') as file:
            for line in file:
                line = line.strip()
                if line:
                    print(line)
                    subprocess.run(line, shell=True)

     
        print("Batch processing complete.")
        yesno = input("delete batch file?  (y/n) ")
        if yesno.lower() == 'y':
            os.remove("./batch.txt")
            print("batch.txt deleted.\n\nReady.")


    except FileNotFoundError:
        print("batch.txt not found.\nFirst set batch mode to True in the GUI, then choose some videos.")
    except Exception as e:
        print(f"An error occurred: {e}")


def batchload():
    thread = threading.Thread(target=_batchloader, daemon=True)
    thread.start()


if __name__ == "__main__":
    batchload() 