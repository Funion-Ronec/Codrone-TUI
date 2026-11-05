from codrone_simulator import *
import sys
import datetime
import os

current_time = datetime.datetime.now().strftime('%Y_%m_%d_%H_%M_%S')

command_log = []

# Swap codrone_edu for codrone_simulator if needed
drone = Drone()
drone.pair()

sys.stdout.reconfigure(encoding="utf-8")
with open("CodroneCliAscii.txt", "r", encoding="utf-8") as file:
    print(file.read())

while True:
    Codrone = input("[Codrone]")
    Codrone_in_parts = Codrone.split()
    if Codrone.startswith("direct"): # need to fix this part later
        if len(Codrone_in_parts) > 1:
            CC = Codrone_in_parts[1]
            drone.CC()
            print(f"{CC} ran")
    elif Codrone == "flip":
        drone.square()
        print("flipped")
    elif Codrone.startswith("cat"): # cat 
        if len(Codrone_in_parts) == 2:
            file_path = Codrone_in_parts[1]
            try:
                with open(file_path, 'r') as file:
                    print(file.read())
            except FileNotFoundError:
                print(f"Error: {file_path} not found.")
        if len(Codrone_in_parts) != 2:
            print("Invalid Argument, Format: cat [File Directory]")
    elif Codrone == "clear": # I lowk dont kmow how this works
        os.system('cls' if os.name == 'nt' else 'clear')
    elif Codrone.startswith("echo"):
        if len(Codrone_in_parts) > 1:
            os.system(f"echo {''.join(Codrone_in_parts[1:])}")
            command_log.append(Codrone)
        else:
            os.system("echo")
            command_log.append(Codrone)
    elif Codrone == "help":
        print("This will be expanded soon")
        print("takeoff | you take off, pretty self explanatory")
        print("land {-e} | you land, pretty self explanatory. -e is for emergency landing")
        print("exit | lands the drone and exits the program")
        command_log.append("help")
    elif Codrone.startswith("pair"): # checks for the command
        if len(Codrone_in_parts) == 2: # check if the user inputed a port number
            portnumber = Codrone_in_parts[1]
            drone.pair(portnumber)
            print(f"{portnumber} is connected")
            command_log.append(Codrone)
        if len(Codrone_in_parts) == 1:
            print("Paired")
            command_log.append(Codrone)
        else:
            drone.pair()
            print("Invalid Argument, format: pair [Port Number]")
            command_log.append(Codrone)
    elif Codrone == "disconnect":
        print("disconnecting")
        drone.close()
        print("drone disconnected")
        command_log.append(Codrone)
    elif Codrone == "takeoff":
        print("taking off")
        drone.takeoff()
        drone.hover()
        command_log.append(Codrone)
    # if Codrone.startswith("go"):


    elif Codrone.startswith("land"):
        if "-e" in Codrone:
            print("landing urgently")
            drone.land() # emergency stop does not exist in codronesim
            print("landed")
            command_log.append(Codrone)
        else: #ill just do ts later lowk frfr
            print("landing")
            drone.land()
            print("landed")
            command_log.append(Codrone)
    elif Codrone == "exit":
        if drone.hover == True: #isflying check does not exist in codronesim
            drone.land()
        print("exiting")
        command_log.append(Codrone)
        os.system(f"echo {command_log} > codrone-command-log-{current_time}.txt")
        break
    else:
        print("unknown command")