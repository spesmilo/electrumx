## Install electrumx docker image for Nengcoin on x86_64 (amd64) or arm64 (aarch64) GNU/linux: 

Pull down a working docker image from docker hub:

x64
```
  docker pull shorelinecrypto/electrumx-neng:amd64
  docker tag shorelinecrypto/electrumx-neng:amd64 electrumx-neng:latest
```
arm64
```
  docker pull shorelinecrypto/electrumx-neng:arm64
  docker tag shorelinecrypto/electrumx-neng:arm64 electrumx-neng:latest
```
Alternatively, build docker image from source:

```
  docker build -t electrumx-neng .
```

### Run electrumx Nengcoin server with docker

Replace with your NENG full node rpcuser/rpcpassword and your server hostname with below command, assuming the NENG full node runs at rpcport=8388 :

```
  docker run -d --net=host -v /opt/electrumx/db-NENG/:/db -v /opt/electrumx/ssl:/ssl -e DAEMON_URL="http://youruser:yourpass@127.0.0.1:8388" -e REPORT_SERVICES=tcp://yourhost:10001,ssl://yourhost:10002,wss://yourhost:10003 electrumx-neng
```

### Trouble shoot or check docker container status

Your docker run electrumx server job should be running, run below to obtain image / container ID

```
  docker ps
  docker container ls -la
  docker images -a
```

In order to trouble shoot issues or check log information of electrumx job, run blow to get real time log information 

```
  docker logs CONTAINER_ID
```

## Shut down electrum Nengcoin docker server
 for a proper clean shutdown, send TERM signal to the running container eg.: 

```
  docker kill --signal="TERM" CONTAINER_ID

```

## clean up and remove residual containers

Stopped containers take up disk and memory resources, you may want to clean up and remove those dead containers to free up resources

```
  docker rm CONTAINER_ID
```

## electrumx server crash maintenance

Electrumx server tend to crash from time to time after running smoothly for several weeks.  Using docker logs CONTAINER_ID method can find 
crash error like this assuming your CONTAINER_ID is 'c39a7d4a07d7': 
```
  docker ps -a
  docker logs c39a7d4a07d7

--- skipped logs informations ---
  struct.error: 'H' format requires 0 <= number <= 65535
```

The ROOT CAUSE of the crash is due to database overflow and can be fixed with below steps  :

#### (1) delete the exited docker container
```
   docker ps -a
   docker rm c39a7d4a07d7
```

#### (2) setup "test" container to do maintenance job
```
    docker run -it --name test  -v /opt/electrumx/db-NENG/:/db   electrumx-neng  /bin/bash
```

This above command will enter docker container with root account.
#### (3) do maintenance in "test" container

Run below commands inside container root account in electrumx folder  /opt/electrumx

```
   python3.10 electrumx_compact_history
   exit
```

The above python3.10 command should take a few minutes to complete and then exit container


#### (4) Delete the containers and re-start electrumx-neng container job
```
   docker rm test
```
   go back to command step above under "Run electrumx Nengcoin server with docker"

