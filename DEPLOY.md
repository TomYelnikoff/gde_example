# Deploying to EC2

## Credentials: use an IAM role, don't copy keys

boto3 looks for credentials in this order: env vars → `~/.aws` files → **EC2 instance metadata (IAM role)**.
On EC2 the role is picked up automatically, inside Docker too. No keys in the image, no keys on the box,
and they rotate on their own.

### 1. Create the IAM role

Trust policy: EC2. Permissions policy (tighten `Resource` once it works):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*"
  }]
}
```

The app calls `converse` / `converse_stream`; these are authorised by the two actions above.
Attach the role to the instance as an instance profile (EC2 → Actions → Security → Modify IAM role).

### 2. Allow containers to reach the metadata service (important)

IMDSv2 defaults to a hop limit of 1, which blocks containers (symptom: "AWS credentials not found").
Set it to 2:

```bash
aws ec2 modify-instance-metadata-options --instance-id i-XXXX \
  --http-put-response-hop-limit 2 --http-tokens required
```

### 3. Model access

In the Bedrock console (us-east-1), make sure access to the Claude model is enabled for the account.

## Get the image onto the instance

**Option A – ECR (recommended)**

```bash
# on your machine
aws ecr create-repository --repository-name bedrock-chatbot --region us-east-1
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com
docker tag bedrock-chatbot:latest <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chatbot:latest
docker push <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chatbot:latest
```

The instance role also needs `ecr:GetAuthorizationToken`, `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer`
(or attach `AmazonEC2ContainerRegistryReadOnly`). Then on the instance:

```bash
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com
docker run -d --name chatbot --restart unless-stopped -p 80:8000 \
  -e AWS_REGION=us-east-1 -v chat-data:/data \
  <ACCOUNT>.dkr.ecr.us-east-1.amazonaws.com/bedrock-chatbot:latest
```

**Option B – no registry**

```bash
docker save bedrock-chatbot:latest | gzip > chatbot.tar.gz
scp chatbot.tar.gz ec2-user@<IP>:~
# on the instance
gunzip -c chatbot.tar.gz | docker load
docker run -d --name chatbot --restart unless-stopped -p 80:8000 -e AWS_REGION=us-east-1 -v chat-data:/data bedrock-chatbot:latest
```

If your laptop is ARM (Apple Silicon) and the instance is x86, build with `docker build --platform linux/amd64 ...`.

## Security: the app has no login

Anyone who can reach the port can chat, and **you pay for every Bedrock call**. Before exposing it:
- Restrict the security group to your office/VPN IP range, **or**
- Put it behind an ALB with Cognito/OIDC authentication, or an authenticating reverse proxy.
- Add HTTPS (ALB + ACM certificate) – chat content is otherwise sent in clear text.

## Data

Conversations live in SQLite on the `chat-data` Docker volume (`/data/chat.db`), so they survive
container restarts and image upgrades. Back it up with e.g.
`docker run --rm -v chat-data:/data -v $PWD:/backup alpine cp /data/chat.db /backup/`.
SQLite means a single instance – don't run several replicas against it.

## Config (environment variables)

`AWS_REGION` (default us-east-1), `BEDROCK_MODEL_ID`, `MAX_TOKENS` (2048), `TEMPERATURE` (0.5), `DB_PATH` (/data/chat.db).

## Local test with your own credentials

```powershell
docker compose -f docker-compose.yml -f docker-compose.local.yml up --build   # http://localhost:8000
```
