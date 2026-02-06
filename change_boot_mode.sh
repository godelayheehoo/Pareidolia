#!/bin/bash

current=$(systemctl get-default)

if [ "$current" = "graphical.target" ]; then
    echo "Currently in GUI mode → switching to terminal"
    sudo systemctl set-default multi-user.target
else
    echo "Currently in terminal mode → switching to GUI"
    sudo systemctl set-default graphical.target
fi

sudo reboot
