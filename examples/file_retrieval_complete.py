import os
from openai import OpenAI

# Requires OPENAI_API_KEY environment variable
client = OpenAI()

def create_vector_store_with_file(file_path: str) -> str:
    """Upload a file and create a vector store for file retrieval."""
    import time

    # First upload the file
    with open(file_path, "rb") as file:
        uploaded_file = client.files.create(
            file=file,
            purpose="assistants"  # Required for file search/retrieval
        )
    print(f"File uploaded successfully. File ID: {uploaded_file.id}")

    # Create a vector store
    vector_store = client.vector_stores.create(
        name="Document Analysis Store"
    )
    print(f"Vector store created. Vector Store ID: {vector_store.id}")

    # Add the file to the vector store
    vector_store_file = client.vector_stores.files.create(
        vector_store_id=vector_store.id,
        file_id=uploaded_file.id
    )
    print(f"File added to vector store. Status: {vector_store_file.status}")

    # Wait for the file to be processed
    print("Waiting for file to be processed...")
    while True:
        vector_store_file = client.vector_stores.files.retrieve(
            vector_store_id=vector_store.id,
            file_id=uploaded_file.id
        )
        print(f"Processing status: {vector_store_file.status}")

        if vector_store_file.status == "completed":
            print("File processing completed!")
            break
        elif vector_store_file.status == "failed":
            raise Exception(f"File processing failed: {vector_store_file}")

        time.sleep(2)  # Wait 2 seconds before checking again

    return vector_store.id

def analyze_file_with_streaming(vector_store_id: str, query: str):
    """Analyze files in a vector store using streaming responses with file retrieval."""
    print(f"Analyzing vector store {vector_store_id} with query: {query}")

    # Request a streaming response using the Responses API with file retrieval
    with client.responses.stream(
        model="gpt-4o",
        input=query,
        tools=[{
            "type": "file_search",
            "vector_store_ids": [vector_store_id]
        }],
        include=["file_search_call.results"]
    ) as stream:
        for event in stream:
            print(f"Response: {event}")

def analyze_file_without_streaming(vector_store_id: str, query: str):
    """Analyze files in a vector store using streaming responses with file retrieval."""
    print(f"Analyzing vector store {vector_store_id} with query: {query}")

    # Request a streaming response using the Responses API with file retrieval
    result = client.responses.create(
            model="gpt-4o",
            input=query,
            tools=[{
                "type": "file_search",
                "vector_store_ids": [vector_store_id]
            }],
            include=["file_search_call.results"]
    )
    print(f"Response: {result}")

# Example usage
if __name__ == "__main__":
    # Create a new vector store with a file and analyze it using Responses API
    sample_file_path = "sample_document.txt"  # Replace with your file path

    # Check if file exists before trying to upload
    if os.path.exists(sample_file_path):
        try:
            print("=== Creating vector store and analyzing file with Responses API ===")
            # Create vector store with the file
            vector_store_id = create_vector_store_with_file(sample_file_path)

            # Analyze the files in the vector store using Responses API
            query = "Please summarize the key points and provide insights from this document."
            analyze_file_with_streaming(vector_store_id, query)
            analyze_file_without_streaming(vector_store_id, query)

        except Exception as e:
            print(f"Error creating vector store or analyzing file: {e}")
    else:
        print(f"Sample file {sample_file_path} not found. Please provide a valid file path.")
        print("You can create a sample text file or use an existing document.")
