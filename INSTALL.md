# How to Set up Electrumx Server with Docker Method

Installation with docker method in linux server is the recommended method. This installation method requires your linux server either on x64 or arm64 hardware to have
docker enabled. Below installation guide is based off runing electrumx server using Nengcoin and Cheetahcoin as example. The docker setup is quite general and would
work for other coins supported by electrumx.

Below electrumx server setup for 2 coins (nengcoin and cheetahcoin) are tested for ubuntu20.04 or ubuntu22.04 on x86_64 or arm64 hardware.


## Run NENG or CHTA Full Node

In the same linux server, you will need to run nengcoin and/or cheetahcoin full node.  Download latest version of NENG or CHTA core wallet, set up proper
rpc username/password and rpc port in the "nengcoin.conf" and/or "cheetahcoin.conf" in their proper wallet folder, make sure the conf file has one line below to insure your full node contain all transactions:

```
txindex=1
```

Run full node and sync the full node to latest block height.

Copy down the rpcuser/rpcpassword/rpcport information, they will be used in below electrumx server configuration.

## Prepare electrumx folders

We run electrumx folder at "/opt/electrumx" in host for mouting db and ssl folder from host to container. You can pick another path as your choice and
change your container launch scripts volume mount point accordingly. 

Ran below:
```
sudo mkdir -p /opt/electrumx/db-NENG
sudo mkdir -p /opt/electrumx/db-CHTA
sudo mkdir -p /opt/electrumx/ssl
```
The above commands created proper folders that will be used in docker run jobs

## Obtain SSL certificate through Certbot/NGINX

SSL or WSS URL connection method is recommended for current Komodo Wallet/Cheetahdex Wallet version and will be required for future versions while self created certificate files do not work for Komodo Wallet/Cheetahdex Wallet.  Here we obtain free letsencrypt certificate through certbot/NGINX.
The below setup steps are largely based off komodo guide on this issue:  https://komodoplatform.com/en/docs/komodo/setup-electrumx-server/

- Dependency - firewall/ port / certbot / nginx

First of all, make sure to open up port 80 if your server is behind firewall because certbot ssl validation requires port 80 through nginx. You may also open up more ports 
on 10001, 10002, 10003 used by Nengcoin TCP/SSL/WSS and ports 10007/10008/10009 used by Cheetahcoin TCP/SSL/WSS electrumx services.

Perform installation of certbot uses snap and installation of nginx uses apt-get in ubuntu 20.04 or ubuntu 22.04:
```
  sudo apt install snapd
  sudo apt install nginx-core
```


Ran below to obtain SSL certificate needed for electrum ssl/wss connection. 
```
sudo snap install core; sudo snap refresh core
sudo apt-get remove certbot
sudo snap install --classic certbot
sudo ln -s /snap/bin/certbot /usr/bin/certbot
sudo certbot --nginx
```

certbot/nginx above command will prompt you for desired subdomain or domain for certificates and will install live copy when process succeed. 
```commandline
cp /etc/letsencrypt/live/electrum2.mooo.com/fullchain.pem  /opt/electrumx/ssl/
cp /etc/letsencrypt/live/electrum2.mooo.com/privkey.pem   /opt/electrumx/ssl/
```

The free letsencrypt certificate is valid for 3 months and you can renew once it expires.


## Install docker

In ubuntu or debian, you can run below to install docker in your linux machine:

```
 sudo apt-get update
 sudo apt-get install -y docker.io
```

Check out online guide if your linux is openSUSE, fedora or arch based.


## Install coin docker images, run docker job

For Nengcoin,  checkout 'contrib/nengcoin' of this repos README guide, follow the guide step by step to install docker image, run a docker-run job, and trouble shoot
job log information or other maintanence tasks if needed.

For Cheetahcoin, checkout 'contrib/cheetahcoin' of this repos README guide, follow the guide step by step to install docker image, run a docker-run job, and trouble shoot
job log information or other maintanence tasks if needed.

Your can run 2 coins with docker jobs together in the same linux server. 

The electrumx server should be running now allow Komodo Wallet/Cheetahdex Wallet, electrum-NENG or electrum-CHTA to connect to your server.
