# Chat with your GPU

Converse with your GPU and get live metrics embedded in copious amounts of snark. 


## Notes
If notice slow token generation from partial CPU/RAM offloading, drop PARAMETER num_ctx 4096 to PARAMETER num_ctx 2048

Run proxy with uvicorn proxy:app --host 0.0.0.0 --port 11435 