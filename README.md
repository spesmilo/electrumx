# Quick installation guide - Ferrite ElectrumX server
Ubuntu 22.04 LTS - username: *ubuntu*  

## Setup
### In *Home* folder, clone the repository.  
ubuntu@ubuntu-virtual-machine:\~$ `git clone https://github.com/koh-gt/electrumx.git`  

### Enter the *electrumx* directory and use pip to install.  
ubuntu@ubuntu-virtual-machine:\~$ `cd electrumx`  
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `pip3 install .`  

### Create a database directory *db* and `chown` the folder. Note the username is *ubuntu*.  
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `mkdir db`  
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `chown ubuntu db`  

## electrumx.service
### Copy the sample *electrumx.service* file to `/etc/systemd/system/`
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `sudo cp contrib/systemd/electrumx.service /etc/systemd/system/`

### Use `nano` to edit and configure the *electrumx.service* file.  
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `sudo nano /etc/systemd/system/electrumx.service`  
Change `ExecStart` line to your `ExecStart=/home/ubuntu/electrumx/electrumx_server` folder directory  
Change `ExecStop` line to your `ExecStop=/home/ubuntu/electrumx/electrumx_rpc` folder directory
Change `User` to your username `User=ubuntu`.  

### The file should look as follows:  
```
[Unit]
Description=Electrumx
After=network.target

[Service]
EnvironmentFile=/etc/electrumx.conf
ExecStart=/home/ubuntu/electrumx/electrumx_server
ExecStop=/home/ubuntu/electrumx/electrumx_rpc -p 8000 stop
User=ubuntu   
LimitNOFILE=8192
TimeoutStopSec=30min

[Install]
WantedBy=multi-user.target
```  
Press `Ctrl+X` to Exit and `Y` to save the modified buffer.  
Press `Enter` to confirm writing to the file.  

## electrumx.conf
### Enter the */electrumx/contrib/systemd/* directory and copy the *electrumx.conf* file to the */etc/* folder.  
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `cd contrib/systemd`  
ubuntu@ubuntu-virtual-machine:\~/electrumx/contrib/systemd$ `sudo cp electrumx.conf /etc/`  

### Use `nano` to edit and configure the *electrumx.conf* file.  
ubuntu@ubuntu-virtual-machine:\~/electrumx/contrib/systemd$ `sudo nano /etc/electrumx.conf`  
Change `DB_DIRECTORY` to your databae directory *db* `DB_DIRECTORY = /home/ubuntu/electrumx/db`  
Change `DAEMON_URL` to `user:password@localhost:9573` where `user` is your Ferrite Core rpc username, `password` is your Ferrite Core rpc password.  
> You may change the `localhost` to another IP address if your Ferrite Core rpc node is not locally hosted. `9573` is the RPC port of Ferrite.

Add a line `SERVICES = tcp://0.0.0.0:50001,rpc://`
> This will broadcast the ElectrumX server on port `50001` via TCP. The IP address `0.0.0.0` means all IP addresses are included.
Remove the `#` to uncomment the `COIN` line.
Change `COIN` to `COIN = Ferrite` to host a Ferrite ElectrumX server.

### The file should look as follows:  
```
# default /etc/electrumx.conf for systemd

# REQUIRED
DB_DIRECTORY = /home/ubuntu/electrumx/db
# Bitcoin Node RPC Credentials
DAEMON_URL = user:password@localhost:9573 

SERVICES = tcp://0.0.0.0:50001,rpc://
COIN = Ferrite 

# See https://electrumx-spesmilo.readthedocs.io/en/latest/environment.html for
# information about other configuration settings you probably want to consider.
```  
Press `Ctrl+X` to Exit and `Y` to save the modified buffer.  
Press `Enter` to confirm writing to the file.  

## Starting the ElectrumX server.  
### Return to the *electrumx* directory and create a symlink to the ElectrumX service  
ubuntu@ubuntu-virtual-machine:\~/electrumx/contrib/systemd$ `cd ..`  
ubuntu@ubuntu-virtual-machine:\~/electrumx/contrib$ ```cd ..``` 
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `sudo systemctl enable electrumx.service`  

### Start the ElectrumX service and check its status. (ensure ferrited is running)
ubuntu@ubuntu-virtual-machine:\~/electrumx$ `sudo systemctl start electrumx.service`  
> Optional: ubuntu@ubuntu-virtual-machine:\~/electrumx$ `sudo systemctl status electrumx.service`  
Open another terminal and run `journalctl -fu electrumx.service` to monitor ElectrumX.

Synchronising  
```
Jul 13 04:31:06 ubuntu-virtual-machine electrumx_server[41863]: INFO:DB:flush #14 took 0.0s.  Height 10,435 txs: 10,812 (+1,053)
Jul 13 04:31:06 ubuntu-virtual-machine electrumx_server[41863]: INFO:BlockProcessor:processed 1,000 blocks size 0.46 MB in 0.0s
Jul 13 04:31:07 ubuntu-virtual-machine electrumx_server[41863]: INFO:DB:flush #15 took 0.0s.  Height 11,435 txs: 11,851 (+1,039)
Jul 13 04:31:07 ubuntu-virtual-machine electrumx_server[41863]: INFO:BlockProcessor:processed 1,000 blocks size 0.35 MB in 0.0s
```






