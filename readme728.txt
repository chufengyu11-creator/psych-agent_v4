首先是进入文件目录里面
其次要开三个终端
1、启动大模型，这个指令是：CUDA_VISIBLE_DEVICES=0,1,2,3,4,5 bash vllm27B.sh CUDA_VISIBLE_DEVICES=6,7 bash vllm0.8B.sh
2、开启网页端，这个指令是：uvicorn app.main:app --host 0.0.0.0 --port 8001
3、查看数据库，这个指令是：psql -h 127.0.0.1 -U psych_agent -d psych_agent   数据库密码是：replace-me  
4、使用网页端进行对话测试，打开此网址：http://111.56.189.29:8000/
