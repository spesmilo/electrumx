#! /bin/bash

if (test $# != 1) 
then
 echo "electrum_maintain.sh  <electrumx-neng|electrumx-chta>" 
 exit
fi


image_name=$1


##obtain exited images

container_id=`docker ps --filter "status=exited"  | grep ${image_name} | cut -f1 -d' '`

if ( test -z ${container_id} )
then
  echo "electrumx server running" >  /dev/null
elif [[ $image_name == "electrumx-neng" ]]
then
  docker rm $container_id
  docker run --rm  -v /opt/electrumx/db-NENG/:/db   ${image_name} python3.10 electrumx_compact_history
  bash /root/docker_run_${image_name}.sh
elif [[ $image_name == "electrumx-chta" ]]
then
  docker rm $container_id
  docker run --rm  -v /opt/electrumx/db-CHTA/:/db   ${image_name} python3.10 electrumx_compact_history
  bash /root/docker_run_${image_name}.sh
else
  echo "wrong argument"
fi
    
