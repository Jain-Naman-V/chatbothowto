import nest_asyncio
import os
import json
import sqlite3
import time
import math
from scrapy.spiders import CrawlSpider, Rule
from scrapy.linkextractors import LinkExtractor
from scrapy.crawler import CrawlerProcess
from scrapy.utils.project import get_project_settings
from scrapy.settings import Settings
from scrapy import signals
from collections import defaultdict
from urllib.parse import urlparse
from datetime import datetime
from fastapi import FastAPI, HTTPException
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from pydantic import BaseModel, HttpUrl
from llama_parse import LlamaParse
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_cohere import CohereEmbeddings
from langchain_community.vectorstores import Chroma
from langchain.schema import Document as LangchainDocument
from typing import List, Optional
from pathlib import Path
from groq import Groq
import requests
from urllib.parse import urlparse
from io import BytesIO
import tempfile
from fastapi.responses import JSONResponse
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from office365.runtime.auth.client_credential import ClientCredential
from office365.sharepoint.client_context import ClientContext
from office365.sharepoint.files.file import File
import boto3
from botocore.exceptions import ClientError
from langchain_groq import ChatGroq
import yfinance as yf
from langchain_core.tools import tool
from datetime import date, timedelta
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage, ToolMessage
import logging
from fastapi.middleware.cors import CORSMiddleware  # ADDED CORS IMPORT  
from scrapy.utils.project import get_project_settings
from scrapy.settings import Settings
from scrapy import signals
from collections import defaultdict
nest_asyncio.apply()

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# Hardcoded API keys (replace with environment variables in production)
GROQ_API_KEY = "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"  # Replace with your Groq API key
LLAMAPARSE_API_KEY = "llx-m7Hjg39mz5mttG3itX73iR7ZQAN1AEDYIiAYj0fskUJ41iwu"  # Replace with your LlamaParse API key
COHERE_API_KEY = "LEndd5GjdPwV26AjGaNKCpSBc0pU3j9pyBE5EMyF"  # Replace with your Cohere API key


#llm = ChatGroq(temperature=0.7, model_name="llama3-70b-8192", groq_api_key=GROQ_API_KEY) # Initialize Groq LLM
llm = ChatGroq(groq_api_key=GROQ_API_KEY, model='llama3-70b-8192')
app = FastAPI()

# ADDED CORS MIDDLEWARE CONFIGURATION
origins = [
    "http://localhost:3000",  # Or the origin of your React app
    # If you need to allow all origins (for testing ONLY, remove in production):
    # "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["POST", "GET", "OPTIONS"],  # or simply ["*"] for all methods during development
    allow_headers=["*"],  # or specify needed headers like ["Content-Type"]
)


@tool
def get_stock_info(symbol, key):
    """
    Return the correct stock info value given the appropriate symbol and key.
    (See full list of valid keys in original code)

    If asked generically for 'stock price', use currentPrice
    """
    data = yf.Ticker(symbol)
    stock_info = data.info
    return stock_info[key]

@tool
def get_historical_price(symbol, start_date, end_date):
    """
    Fetches historical stock prices for a given symbol from 'start_date' to 'end_date'.
    - symbol (str): Stock ticker symbol.
    - end_date (date): Typically today unless a specific end date is provided.
                      End date MUST be greater than start date
    - start_date (date): Set explicitly, or calculated as 'end_date - date interval'
                        (for example, if prompted 'over the past 6 months',
                         date interval = 6 months so start_date would be 6 months
                         earlier than today's date). Default to '1900-01-01'
                         if vaguely asked for historical price. Start date must
                         always be before the current date
    """
    data = yf.Ticker(symbol)
    hist = data.history(start=start_date, end=end_date)
    hist = hist.reset_index()
    hist[symbol] = hist['Close']
    return hist[['Date', symbol]]
tools = [get_stock_info, get_historical_price]
llm_with_tools = llm.bind_tools(tools)

def get_stock_analysis(user_prompt):
    system_prompt = 'You are a helpful finance assistant that analyzes stocks and stock prices. Today is {today}'.format(today=date.today())
    messages = [SystemMessage(system_prompt), HumanMessage(user_prompt)]
    ai_msg = llm_with_tools.invoke(messages)
    messages.append(ai_msg)

    for tool_call in ai_msg.tool_calls:
        selected_tool = {"get_stock_info": get_stock_info, "get_historical_price": get_historical_price}[tool_call["name"].lower()]
        tool_output = selected_tool.invoke(tool_call["args"])
        messages.append(ToolMessage(str(tool_output), tool_call_id=tool_call["id"]))

    return llm_with_tools.invoke(messages).content

class StockAnalysisModel(BaseModel):
    query: str

# In-memory chat history storage
chat_history_store = {}
MAX_HISTORY_LENGTH = 5

def manage_chat_history(user_id: str, session_id: str, message: str):
    """
    Manages chat history in memory.  Stores up to MAX_HISTORY_LENGTH messages
    per user/session combination.  Logs messages.
    """
    key = f"{user_id}:{session_id}"
    if key not in chat_history_store:
        chat_history_store[key] = []

    if message:  # Only add non-empty messages
        chat_history_store[key].append(message)
        logger.info(f"User: {user_id}, Session: {session_id}, Message: {message}")

    # Keep only the last MAX_HISTORY_LENGTH messages
    chat_history_store[key] = chat_history_store[key][-MAX_HISTORY_LENGTH:]

    # Return the history in chronological order (oldest first)
    return "\n".join(chat_history_store[key])

# Initialize global variables
vs_dict = {}

# Define a directory to store uploaded files
UPLOAD_DIRECTORY = "uploaded_files"

# Ensure the upload directory exists
os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)

# New Pydantic model for link processing
class LinkProcessModel(BaseModel):
    link: HttpUrl
    collection_name: str
    process_all: bool = True  # True to process all files, False for single file
    file_name: Optional[str] = None  # Specify file name if processing single file

def determine_service(url):
    domain = urlparse(url).netloc
    if 'drive.google.com' in domain:
        return 'google_drive'
    elif 'sharepoint.com' in domain:
        return 'sharepoint'
    elif 's3.amazonaws.com' in domain:
        return 'amazon_s3'
    elif 'box.com' in domain:
        return 'box'
    else:
        return 'unknown'
# Initialize the Groq client
client = Groq(api_key=GROQ_API_KEY)



# Helper function to load and parse the input data
def mariela_parse(files):
    parser = LlamaParse(
        api_key=LLAMAPARSE_API_KEY,
        result_type="markdown",
        verbose=True
    )
    parsed_documents = []
    for file in files:
        parsed_documents.extend(parser.load_data(file))
    return parsed_documents

# Create vector database
def mariela_create_vector_database(parsed_documents, collection_name):
    langchain_docs = [
        LangchainDocument(page_content=doc.text, metadata=doc.metadata)
        for doc in parsed_documents
    ]

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=5000, chunk_overlap=100)
    docs = text_splitter.split_documents(langchain_docs)

    embed_model = CohereEmbeddings(model="embed-multilingual-v3.0", cohere_api_key=COHERE_API_KEY)

    vs = Chroma.from_documents(
        documents=docs,
        embedding=embed_model,
        persist_directory="chroma_db",
        collection_name=collection_name
    )

    return vs

# Function to manage chat history

def process_google_drive(link, process_all, file_name=None):
    # This is a simplified version. In a real scenario, you'd need to handle OAuth 2.0
    #  Replace 'path/to/credentials.json' with your actual credentials file path.
    credentials = Credentials.from_authorized_user_file('path/to/credentials.json') # Requires proper setup
    drive_service = build('drive', 'v3', credentials=credentials)

    file_id = link.split('/')[-1]
    if process_all:
        results = drive_service.files().list(q=f"'{file_id}' in parents", fields="files(id, name)").execute()
        files = results.get('files', [])
    else:
        files = [{'id': file_id, 'name': file_name}]

    downloaded_files = []
    for file in files:
        request = drive_service.files().get_media(fileId=file['id'])
        fh = BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            status, done = downloader.next_chunk()
        fh.seek(0)
        downloaded_files.append((file['name'], fh))

    return downloaded_files

# Function to process SharePoint
def process_sharepoint(link, process_all, file_name=None):
    # You'd need to set up authentication with client credentials  Replace with your actual credentials
    client_credentials = ClientCredential("YOUR_SHAREPOINT_CLIENT_ID", "YOUR_SHAREPOINT_CLIENT_SECRET") #Requires proper setup
    ctx = ClientContext(link).with_credentials(client_credentials)

    if process_all:
        folder = ctx.web.get_folder_by_server_relative_url(link)
        files = folder.files
        ctx.load(files)
        ctx.execute_query()
    else:
        files = [File.from_url(ctx, f"{link}/{file_name}")]

    downloaded_files = []
    for file in files:
        content = file.read()
        downloaded_files.append((file.properties['Name'], BytesIO(content)))

    return downloaded_files

# Function to process Amazon S3
def process_s3(link, process_all, file_name=None):
    # Replace with your AWS credentials.  Recommended to use environment variables or IAM roles.
    s3 = boto3.client('s3') #Requires proper AWS setup
    bucket_name, key = link.split('/', 3)[2:]

    if process_all:
        objects = s3.list_objects_v2(Bucket=bucket_name, Prefix=key)['Contents']
    else:
        objects = [{'Key': f"{key}/{file_name}"}]

    downloaded_files = []
    for obj in objects:
        file = BytesIO()
        s3.download_fileobj(bucket_name, obj['Key'], file)
        file.seek(0)
        downloaded_files.append((obj['Key'].split('/')[-1], file))

    return downloaded_files
# Endpoint models
class FileUploadModel(BaseModel):
    filepath: str
    collection_name: str

class QueryModel(BaseModel):
    collection_name: str
    query: str
    user_id: str
    session_id: str

class TransformationModel(BaseModel):
    user_query: str
class WebsiteSpider(CrawlSpider):
    name = 'website_spider'
    
    def __init__(self, start_url=None, allowed_domains=None, *args, **kwargs):
        self.start_urls = [start_url]
        self.allowed_domains = [allowed_domains] if allowed_domains else [urlparse(start_url).netloc]
        
        # Custom settings for the spider
        self.custom_settings = {
            'USER_AGENT': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'ROBOTSTXT_OBEY': True,
            'CONCURRENT_REQUESTS': 16,
            'DOWNLOAD_DELAY': 0.5,  # Add delay to be respectful to the server
            'COOKIES_ENABLED': False,
            'HTTPCACHE_ENABLED': True,
            'HTTPCACHE_EXPIRATION_SECS': 60 * 60 * 24,  # Cache for 24 hours
            'HTTPCACHE_DIR': 'httpcache',
            'DEPTH_LIMIT': 3,  # Limit crawling depth to 3 levels
        }
        
        # Define rules for link extraction
        self.rules = (
            Rule(LinkExtractor(allow=(), deny=('logout', 'sign-out', 'login', 'sign-in', 'pdf', 'doc', 'xls', 'ppt', 'zip', 'rar')), 
                 callback='parse_item', follow=True),
        )
        
        self.scraped_data = []
        super(WebsiteSpider, self).__init__(*args, **kwargs)
    
    def parse_item(self, response):
        # Extract page content
        title = response.css('title::text').get() or ''
        body_text = ' '.join(response.css('body ::text').getall())
        
        # Clean up the text
        body_text = ' '.join(body_text.split())
        
        # Store the extracted data
        self.scraped_data.append({
            'url': response.url,
            'title': title.strip(),
            'content': body_text
        })
        
        return {
            'url': response.url,
            'title': title.strip(),
            'content': body_text
        }

# Add this class to your existing models
class WebscrapeModel(BaseModel):
   Webscrape url: str
    collection_name: str
    max_pages: int = 50  # Maximum number of pages to scrape

@app.post("/webscrape/")
async def webscrape(data: WebscrapeModel):
    """
    Endpoint to scrape a website and store the content in a text file,
    then parse it through LlamaParse into a vector database.
    Rate limited to send only 5 pages per minute to Cohere.
    """
    url = data.url
    collection_name = data.collection_name
    max_pages = data.max_pages
    
    try:
        # Create a temporary directory to store scraped data
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create settings for the crawler
            settings = Settings()
            settings.set('FEEDS', {
                os.path.join(temp_dir, 'output.json'): {
                    'format': 'json',
                    'encoding': 'utf8',
                    'indent': 4,
                }
            })
            settings.set('USER_AGENT', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36')
            settings.set('ROBOTSTXT_OBEY', True)
            settings.set('CONCURRENT_REQUESTS', 16)
            settings.set('DOWNLOAD_DELAY', 0.5)
            settings.set('COOKIES_ENABLED', False)
            settings.set('HTTPCACHE_ENABLED', True)
            settings.set('HTTPCACHE_EXPIRATION_SECS', 60 * 60 * 24)
            settings.set('HTTPCACHE_DIR', 'httpcache')
            settings.set('CLOSESPIDER_PAGECOUNT', max_pages)  # Stop after max_pages
            
            # Create and run the crawler
            process = CrawlerProcess(settings)
            domain = urlparse(url).netloc
            
            # Store crawled data
            items = defaultdict(list)
            
            # Save references to all items
            def collect_item(item, response, spider):
                items[spider.name].append(item)
            
            # Configure the crawler
            process.crawl(WebsiteSpider, start_url=url, allowed_domains=domain)
            for crawler in process.crawlers:
                crawler.signals.connect(collect_item, signal=signals.item_scraped)
            
            process.start()  # This will block until crawling is finished
            
            # Create a combined text file from all the scraped data
            output_file = os.path.join(temp_dir, f"{collection_name}_scraped_content.txt")
            with open(output_file, 'w', encoding='utf-8') as f:
                for spider_name, spider_items in items.items():
                    for item in spider_items:
                        f.write(f"URL: {item['url']}\n")
                        f.write(f"TITLE: {item['title']}\n")
                        f.write(f"CONTENT:\n{item['content']}\n")
                        f.write("\n---\n\n")
            
            # Count the number of pages scraped
            total_pages = sum(len(items[name]) for name in items)
            logger.info(f"Total pages scraped: {total_pages}")
            
            # Now parse the file using LlamaParse
            parsed_documents = mariela_parse([output_file])
            
            # Process documents in batches of 5 per minute to limit Cohere API calls
            BATCH_SIZE = 5
            batches = math.ceil(len(parsed_documents) / BATCH_SIZE)
            
            logger.info(f"Processing {len(parsed_documents)} documents in {batches} batches")
            
            # Create a temporary Chroma collection to build incrementally
            embed_model = CohereEmbeddings(model="embed-multilingual-v3.0", cohere_api_key=COHERE_API_KEY)
            text_splitter = RecursiveCharacterTextSplitter(chunk_size=5000, chunk_overlap=100)
            
            vs = None
            
            for i in range(batches):
                start_idx = i * BATCH_SIZE
                end_idx = min((i + 1) * BATCH_SIZE, len(parsed_documents))
                batch = parsed_documents[start_idx:end_idx]
                
                logger.info(f"Processing batch {i+1}/{batches} with {len(batch)} documents")
                
                # Convert to Langchain documents
                langchain_docs = [
                    LangchainDocument(page_content=doc.text, metadata=doc.metadata)
                    for doc in batch
                ]
                
                # Split documents
                docs = text_splitter.split_documents(langchain_docs)
                
                # Create or update vector store
                if vs is None:
                    vs = Chroma.from_documents(
                        documents=docs,
                        embedding=embed_model,
                        persist_directory="chroma_db",
                        collection_name=collection_name
                    )
                else:
                    vs.add_documents(docs)
                
                # Sleep for the remainder of the minute if not the last batch
                if i < batches - 1:
                    logger.info("Rate limiting: Sleeping for 60 seconds before processing next batch")
                    time.sleep(60)
            
            # Store the vector store reference
            vs_dict[collection_name] = vs
            
            return {
                "message": f"Successfully scraped {total_pages} pages from {url}",
                "collection_name": collection_name,
                "pages_scraped": total_pages,
                "batches_processed": batches
            }
            
    except Exception as e:
        logger.error(f"Error scraping website: {e}")
        raise HTTPException(status_code=500, detail=f"Error scraping website: {str(e)}")


@app.post("/use_api/")
async def stock_analysis(data: StockAnalysisModel):
    user_query = data.query
    analysis = get_stock_analysis(user_query)
    return {"analysis": analysis}

# Endpoint: Single file upload
@app.post("/upload_file/")
async def upload_file(data: FileUploadModel):
    global vs_dict
    filepath = data.filepath
    collection_name = data.collection_name

    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail=f"File {filepath} not found")

    parsed_documents = mariela_parse([filepath])
    vs = mariela_create_vector_database(parsed_documents, collection_name)

    vs_dict[collection_name] = vs
    return {"message": f"File uploaded, parsed, and stored in collection: {collection_name}"}

# Endpoint: Directory upload
@app.post("/upload_directory/")
async def upload_directory(data: FileUploadModel):
    global vs_dict
    directory = data.filepath
    collection_name = data.collection_name

    if not os.path.exists(directory) or not os.path.isdir(directory):
        raise HTTPException(status_code=404, detail=f"Directory {directory} not found")

    files = [str(file) for file in Path(directory).glob("*") if file.is_file()]
    if not files:
        raise HTTPException(status_code=400, detail=f"No valid files found in {directory}")

    parsed_documents = mariela_parse(files)
    vs = mariela_create_vector_database(parsed_documents, collection_name)

    vs_dict[collection_name] = vs
    return {"message": f"Directory uploaded, parsed, and stored in collection: {collection_name}"}
@app.post("/process_db/")
async def process_corporate_link(data: LinkProcessModel):
    service = determine_service(str(data.link))
    if service == 'unknown':
        raise HTTPException(status_code=400, detail="Unsupported file hosting service")

    try:
        if service == 'google_drive':
            files = process_google_drive(str(data.link), data.process_all, data.file_name)
        elif service == 'sharepoint':
            files = process_sharepoint(str(data.link), data.process_all, data.file_name)
        elif service == 'amazon_s3':
            files = process_s3(str(data.link), data.process_all, data.file_name)

        processed_files = []
        for file_name, file_content in files:
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(file_content.getvalue())
                temp_file_path = temp_file.name

            parsed_documents = mariela_parse([temp_file_path])
            vs = mariela_create_vector_database(parsed_documents, f"{data.collection_name}_{file_name}")

            vs_dict[f"{data.collection_name}_{file_name}"] = vs
            processed_files.append(file_name)

            os.unlink(temp_file_path)

        return {
            "message": f"Files from {service} processed and stored in collections",
            "processed_files": processed_files
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file(s): {str(e)}")

@app.post("/upload_local_file/")
async def upload_local_file(
    file: UploadFile = File(...),
    collection_name: str = Form(...)
):
    """
    Endpoint to handle file uploads from user's local machine.
    """
    try:
        # Create a unique filename to avoid overwriting
        file_extension = os.path.splitext(file.filename)[1]
        unique_filename = f"{collection_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{file_extension}"
        file_path = os.path.join(UPLOAD_DIRECTORY, unique_filename)

        # Save the file locally
        with open(file_path, "wb") as buffer:
            contents = await file.read()
            buffer.write(contents)

        # Process the saved file
        parsed_documents = mariela_parse([file_path])
        vs = mariela_create_vector_database(parsed_documents, collection_name)
        vs_dict[collection_name] = vs

        return JSONResponse(content={
            "message": f"File '{file.filename}' uploaded and processed successfully.",
            "collection_name": collection_name,
            "saved_as": unique_filename
        }, status_code=200)
    except Exception as e:  # Catch any exception
        logger.error(f"Error processing uploaded file: {e}")  # Log the error
        raise HTTPException(status_code=500, detail=f"Error processing file: {e}")

# Endpoint: Retrieval
@app.post("/retrieve/")
async def retrieve(data: QueryModel):
    global vs_dict
    collection_name = data.collection_name
    query = data.query

    if collection_name not in vs_dict:
        raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found")

    vs = vs_dict[collection_name]
    results = vs.similarity_search(query, k=4)

    formatted_results = []
    for i, doc in enumerate(results, 1):
        formatted_results.append({
            "result_number": i,
            "page_content": doc.page_content,
            "metadata": doc.metadata
        })

    return {"results": formatted_results}

# Query Transformation
@app.post("/query_transformation/")
async def query_transformation(data: TransformationModel):
    user_query = data.user_query

    system_message = """
    You are an advanced assistant that breaks down complex questions into simpler sub-questions.
    Your goal is to take a user query and generate 4 sub-queries that will help answer the original question.
    Return the sub-queries in the following JSON format:

    {
        "sub_queries": [
            {"sub_query_1": "<first sub-question>"},
            {"sub_query_2": "<second sub-question>"},
            {"sub_query_3": "<third sub-question>"},
            {"sub_query_4": "<fourth sub-question>"}
        ]
    }
    """

    messages = [
        {
            "role": "system",
            "content": system_message
        },
        {
            "role": "user",
            "content": f"Generate 4 sub-questions for the query: '{user_query}'"
        }
    ]

    completion = client.chat.completions.create(
        model="llama-3.2-1b-preview",
        messages=messages,
        temperature=0.7,
        max_tokens=1024,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
        stop=None,
    )

    sub_queries_json = completion.choices[0].message.content

    return json.loads(sub_queries_json)

# Query answering function with chat history
def query_answer(user_query: str, retrieved_docs: str, user_id: str, session_id: str):
    chat_history = manage_chat_history(user_id, session_id, f"Q: {user_query}")

    system_message = """
    You are an advanced voice based chatbot that provides answers based on the given documents and chat history.
    User can ask to questions which might need reference to the documents or previous chat history , so do some reasoning and form an answer
    If the answer to the user's query is not explicitly present in the documents or chat history, respond with this ONLY:
    "I am sorry i dont think i have information on that , could you ask something else?."
    Do not provide any answers from your general knowledge, only use the given documents and chat history.
    your answer should be precise and only answering the question not talking about anything else extra like based on documents or based on chat history.
    since this is a voice based conversatation limit your answer to 50 to 100 words at maximum , also dont include \n in your answers or format it , it should be pure raw text
    """

    context = f"Chat History (previous questions and answers):\n{chat_history}\n\nDocuments:\n{retrieved_docs}"

    messages = [
        {
            "role": "system",
            "content": system_message
        },
        {
            "role": "user",
            "content": f"User query: {user_query}\n\nContext:\n{context}"
        }
    ]

    completion = client.chat.completions.create(
        model="llama-3.3-70b-specdec",
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
        top_p=1,
        stream=False,
        stop=None,
    )

    answer = completion.choices[0].message.content
    manage_chat_history(user_id, session_id, f"A: {answer}")

    return answer

# Grounding agent function
def grounding_agent(answer, question, context, user_id, session_id):
    chat_history = manage_chat_history(user_id, session_id, "")
    grounding_system_message = """
    You are a grounding agent. Your task is to evaluate the given answer based on the provided question and context.
    You must return a JSON object in the following format:
    {
        "is_hallucinated": 0 or 1,  # 0 means the answer is grounded, 1 means it is hallucinated
        "explanation": "A brief explanation of why the answer is or is not hallucinated"
    }
    Analyze if the answer is directly derived from the provided documents.
    """
    full_context = f"Chat History (previous questions and answers):\n{chat_history}\n\nContext: {context}"
    messages = [
        {
            "role": "system",
            "content": grounding_system_message
        },
        {
            "role": "user",
            "content": f"Question: {question}\n{full_context}\nAnswer: {answer}"
        }
    ]

    completion = client.chat.completions.create(
        model="llama-3.3-70b-specdec",
        messages=messages,
        temperature=0.7,
        max_tokens=1024,
        top_p=1,
        stream=False,
        response_format={"type": "json_object"},
        stop=None,
    )

    grounding_result_str = completion.choices[0].message.content
    return json.loads(grounding_result_str)

# Chat endpoint
@app.post("/chat/")
async def chat(data: QueryModel):
    collection_name = data.collection_name
    user_query = data.query
    user_id = data.user_id
    session_id = data.session_id
    if collection_name not in vs_dict:
        raise HTTPException(status_code=404, detail=f"Collection '{collection_name}' not found")


    # Step 2: Retrieve documents for each sub-query
    vs = vs_dict[collection_name]
    all_retrieved_docs = []

    results = vs.similarity_search(user_query, k=4)  # Retrieve 4 docs
    all_retrieved_docs.extend(results)

    # Combine all retrieved documents
    retrieved_docs = "\n\n".join([doc.page_content for doc in all_retrieved_docs])

    # Step 3: RAG with grounding check and chat history
    answer = query_answer(user_query, retrieved_docs, user_id, session_id)
    return {
        "original_query": user_query,
        "answer": answer
    }
