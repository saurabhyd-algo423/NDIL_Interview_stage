# NDIL AI Interviewer 🤖

An intelligent, voice-based recruitment interview system powered by Azure AI services and OpenAI. This application automates the initial screening interviews, evaluates candidate responses in real-time, and generates comprehensive interview reports.

## 📋 Table of Contents

- [Features](#features)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Deployment](#deployment)
- [API Endpoints](#api-endpoints)
- [Project Structure](#project-structure)
- [Technology Stack](#technology-stack)
- [Contributing](#contributing)
- [License](#license)

## ✨ Features

### Core Functionality
- **AI-Powered Interviews**: Conducts automated recruitment interviews using OpenAI's GPT models
- **Voice Interaction**: Real-time speech-to-text and text-to-speech using Azure Speech Services
- **Multi-Phase Interviews**: Structured interview flow with role confirmation, technical questions, behavioral questions, and closing
- **Semantic Voice Activity Detection (VAD)**: Intelligent pause detection to determine when candidates finish speaking
- **Real-Time AI Responses**: Streaming AI responses for natural conversation flow
- **Automatic Report Generation**: Post-interview evaluation and PDF report generation

### Data Management
- **Resume Upload & Storage**: Upload and store resumes in Azure Blob Storage
- **Interview Transcripts**: Automatic transcription and storage of interview conversations
- **Cosmos DB Integration**: Persistent storage for user data, job descriptions, and interview sessions
- **Session Management**: Track interview progress and candidate responses

### Accessibility & Performance
- **Cross-Browser Support**: Works on modern web browsers with WebRTC capabilities
- **Responsive Design**: Mobile-friendly interface
- **Error Handling**: Graceful degradation and comprehensive error messages
- **Health Checks**: Built-in diagnostics and monitoring endpoints

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend (Browser)                       │
│        HTML5 | CSS3 | JavaScript | WebRTC Audio API             │
└────────────────────────────┬────────────────────────────────────┘
                             │
                    HTTP/WebSocket
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                    Flask Backend (Python)                        │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Routes Layer                                              │   │
│  │ • routes_avatar.py    - Avatar & config endpoints        │   │
│  │ • routes_interview.py - Interview flow endpoints         │   │
│  │ • routes_debug.py     - Debug & health check             │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Business Logic Layer                                      │   │
│  │ • interview_session.py - Interview state & flow           │   │
│  │ • speech_service.py    - Speech token & ICE mgmt         │   │
│  │ • evaluator.py         - AI evaluation & scoring          │   │
│  └──────────────────────────────────────────────────────────┘   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Integration Layer                                         │   │
│  │ • cosmos_db_connector.py - Database operations           │   │
│  │ • blob_storage.py        - File storage operations       │   │
│  │ • config.py              - Environment & shared state    │   │
│  └──────────────────────────────────────────────────────────┘   │
└────────────────┬─────────────┬──────────────────┬────────────────┘
                 │             │                  │
      ┌──────────▼───┐  ┌──────▼────────┐  ┌─────▼──────────┐
      │  Azure       │  │  Azure        │  │   Azure        │
      │  Speech      │  │  Cosmos DB    │  │   Blob         │
      │  Services    │  │  (Database)   │  │   Storage      │
      └──────────────┘  └───────────────┘  └────────────────┘
           │
      ┌────▼──────────┐
      │   OpenAI      │
      │   GPT Models  │
      └───────────────┘
```

## 📋 Prerequisites

### System Requirements
- **Python**: 3.10 or higher
- **Node.js**: 16+ (for frontend tooling, optional)
- **Docker**: 20.10+ (for containerized deployment)
- **Git**: For version control

### Azure Services Required
- **Azure Speech Services**: For speech recognition and synthesis
- **Azure Cosmos DB**: For data persistence (NoSQL database)
- **Azure Blob Storage**: For resume and transcript storage
- **Azure OpenAI Services**: For AI-powered interview logic

### Credentials & Keys
- Azure Speech Services API key and region
- Azure Cosmos DB endpoint and key
- Azure Storage Account connection string
- Azure OpenAI API key and endpoint
- OpenAI API key (if not using Azure OpenAI)

## 🚀 Installation

### 1. Clone the Repository

```bash
git clone <repository-url>
cd current_directory
```

### 2. Create a Virtual Environment (Python)

```bash
# Windows
python -m venv venv
venv\Scripts\activate

# macOS/Linux
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

### 4. Create a `.env` File

Create a `.env` file in the root directory with the following variables:

```env
# Azure Speech Services
SPEECH_KEY=your-speech-api-key
SPEECH_REGION=your-speech-region

# Azure Cosmos DB
COSMOS_ENDPOINT=your-cosmos-db-endpoint
COSMOS_DATABASE=your-database-name
COSMOS_USERS_CONTAINER=users
COSMOS_RESUME_CONTAINER=resumes
COSMOS_JD_CONTAINER=job_descriptions

# Azure Blob Storage
BLOB_CONNECTION_STRING=your-blob-connection-string
BLOB_CONTAINER_NAME=interview-data

# Azure OpenAI
AZURE_OAI_KEY=your-azure-openai-key
AZURE_OAI_ENDPOINT=your-azure-openai-endpoint
AZURE_OAI_DEPLOYMENT=your-deployment-name

# OpenAI (Alternative)
OPENAI_API_KEY=your-openai-api-key

# Flask
FLASK_SECRET=your-random-secret-key

# Optional: Interview Configuration
SEMANTIC_VAD_MIN_WORDS=3
INTERVIEW_TIMEOUT=3600
```

## ⚙️ Configuration

### Environment Variables

All configuration is managed through environment variables (loaded via `python-dotenv`). See the `.env` file setup above for all available options.

### Interview Phases

The system supports the following interview phases (defined in `interview_session.py`):

1. **Role Confirmation**: Verifies candidate's understanding of the role
2. **Identity Check**: Basic identity confirmation
3. **Technical Questions**: Role-specific technical assessment
4. **Behavioral Questions**: Soft skills and experience evaluation
5. **Closing**: Opportunity for candidate questions and closing remarks

### Voice Activity Detection (VAD)

- Minimum words threshold: Configurable via `SEMANTIC_VAD_MIN_WORDS`
- Uses semantic analysis to detect meaningful pauses
- Prevents interruption of incomplete thoughts

## 🏃 Running the Application

### Development Mode

```bash
cd backend
python app.py
```

The application will start on `http://localhost:5000`

### Production with Docker

```bash
# Build the Docker image
docker build -t ndil-ai-interviewer:latest .

# Run the container
docker run -p 5000:5000 \
  --env-file .env \
  ndil-ai-interviewer:latest
```

## 📦 Deployment

### Deploying to Azure Container Instances (ACI)

```bash
# Create a resource group
az group create --name ndil-rg --location eastus

# Build and push to Azure Container Registry
az acr build --registry <your-registry-name> \
  --image ndil-ai-interviewer:latest .

# Deploy to Container Instances
az container create --resource-group ndil-rg \
  --name ndil-interviewer \
  --image <your-registry>.azurecr.io/ndil-ai-interviewer:latest \
  --cpu 2 --memory 4 \
  --ports 5000 \
  --environment-variables \
    SPEECH_KEY=<key> \
    SPEECH_REGION=<region> \
    COSMOS_ENDPOINT=<endpoint> \
    AZURE_OAI_KEY=<key> \
    AZURE_OAI_ENDPOINT=<endpoint>
```

### Deploying to Azure App Service

```bash
# Create an App Service plan
az appservice plan create --name ndil-plan \
  --resource-group ndil-rg \
  --sku B2

# Create the web app
az webapp create --name ndil-interviewer \
  --resource-group ndil-rg \
  --plan ndil-plan

# Deploy the application
az webapp deployment source config-zip \
  --resource-group ndil-rg \
  --name ndil-interviewer \
  --src app.zip
```

## 🔌 API Endpoints

### Avatar & Configuration
- **GET** `/` - Serves the main interview interface
- **GET** `/api/config` - Returns system configuration
- **GET** `/api/getSpeechToken` - Provides Azure Speech Service token
- **POST** `/api/startSession` - Initializes a new interview session

### Interview Flow
- **POST** `/api/startInterview` - Begins the interview process
- **POST** `/api/userResponse` - Submits candidate response
- **GET** `/api/getPhaseContext` - Retrieves current interview phase information
- **POST** `/api/endInterview` - Concludes the interview

### Data Management
- **POST** `/api/uploadResume` - Upload candidate resume
- **GET** `/api/getUserData/<user_id>` - Retrieve user information
- **POST** `/api/saveJobDescription` - Store job description for interview

### Debug & Monitoring
- **GET** `/api/healthcheck` - System health status
- **GET** `/api/debug/sessions` - Current active sessions (debug only)
- **GET** `/api/debug/config` - Configuration details (debug only)

## 📂 Project Structure

```
current_directory/
├── backend/
│   ├── app.py                    # Flask application entry point
│   ├── config.py                 # Environment variables & shared state
│   ├── interview_session.py       # Interview business logic & state management
│   ├── speech_service.py          # Azure Speech Services integration
│   ├── evaluator.py               # AI evaluation & scoring
│   ├── cosmos_db_connector.py     # Cosmos DB operations
│   ├── blob_storage.py            # Azure Blob Storage operations
│   ├── routes_avatar.py           # Avatar & configuration routes
│   ├── routes_interview.py        # Interview flow routes
│   ├── routes_debug.py            # Debug & health check routes
│   ├── function.py                # Utility functions
│   └── reports/                   # Generated interview reports
├── frontend/
│   ├── templates/
│   │   └── index.html             # Main interview interface
│   └── static/
│       ├── interview.css          # Styling
│       ├── interview.js           # Frontend logic
│       └── vad-processor.js       # Voice Activity Detection
├── Dockerfile                     # Docker container configuration
├── requirements.txt               # Python dependencies
├── .env                           # Environment variables (create this)
└── README.md                      # This file
```

## 🛠️ Technology Stack

### Backend
- **Python 3.10+**
- **Flask 3.1+** - Web framework
- **Pydantic 2.1+** - Data validation
- **python-dotenv** - Environment management

### Frontend
- **HTML5** - Markup
- **CSS3** - Styling
- **JavaScript (ES6+)** - Client-side logic
- **WebRTC API** - Real-time audio capture

### Azure Services
- **Azure Speech Services** - Speech recognition & synthesis
- **Azure Cosmos DB** - NoSQL database
- **Azure Blob Storage** - File storage
- **Azure OpenAI** - AI model deployment

### AI & ML
- **OpenAI GPT Models** - Interview logic and evaluation
- **Azure Cognitive Services** - Speech processing

### DevOps
- **Docker** - Containerization
- **Azure Container Registry** - Image repository
- **Azure Container Instances/App Service** - Deployment

## 🤝 Contributing

We welcome contributions! Please follow these guidelines:

1. **Fork the repository** and create a feature branch
2. **Follow PEP 8** for Python code style
3. **Add tests** for new functionality
4. **Update documentation** as needed
5. **Submit a pull request** with a clear description

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Troubleshooting

### Common Issues

**Issue: "Speech token failed to generate"**
- Check that `SPEECH_KEY` and `SPEECH_REGION` are correctly configured
- Verify Azure Speech Services resource is active

**Issue: "Cannot connect to Cosmos DB"**
- Verify `COSMOS_ENDPOINT` format (should include protocol and .documents.azure.com)
- Check database and container names match configuration
- Ensure firewall rules allow your IP

**Issue: "OpenAI API errors"**
- Verify API keys and endpoints are correct
- Check deployment name matches Azure OpenAI setup
- Monitor API quota and rate limits

**Issue: "Blob Storage connection failed"**
- Verify `BLOB_CONNECTION_STRING` is correct
- Check container name exists
- Ensure storage account firewall allows access

### Health Check

```bash
# Check if the application is running
curl http://localhost:5000/api/healthcheck
```

## 📚 Documentation

For detailed documentation on specific modules, see:
- [Backend Architecture](backend/README.md) - Detailed backend design
- [API Reference](docs/API.md) - Complete API documentation
- [Azure Setup Guide](docs/AZURE_SETUP.md) - Step-by-step Azure configuration

## 📞 Support

For issues, questions, or suggestions:
- Open an GitHub issue
- Contact the development team
- Check existing documentation and FAQs

---

**Last Updated**: May 2026  
**Version**: 1.0.0  
**Status**: Production Ready ✅
