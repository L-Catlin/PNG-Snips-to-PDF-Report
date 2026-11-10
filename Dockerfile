FROM apify/actor-python:3.11

COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

COPY . ./

CMD ["python", "-u", "-m", "my_actor"]