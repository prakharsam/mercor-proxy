from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import time
import asyncio
from collections import deque
from dataclasses import dataclass
from typing import List, Dict

CLASSIFICATION_SERVER_URL = "http://localhost:8001/classify"

app = FastAPI(
    title="Optimal Batching Proxy",
    description="Optimal batching that sorts by length to minimize latency"
)

class ProxyRequest(BaseModel):
    """Request model for single text classification"""
    sequence: str

class ProxyResponse(BaseModel):
    """Response model containing classification result ('code' or 'not code')"""
    result: str

@dataclass
class PendingRequest:
    """Represents a pending request with its metadata"""
    sequence: str
    timestamp: float
    length: int
    future: asyncio.Future

class OptimalBatchingProxy:
    """Optimal batching proxy that sorts by length to minimize latency"""
    
    def __init__(self):
        self.pending_requests: Dict[str, PendingRequest] = {}
        self.request_queue = deque()
        self.batch_processor_task = None
        self.running = True
        
        # Single HTTP client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_keepalive_connections=1, max_connections=1)
        )
    
    async def start(self):
        """Start the batch processing loop"""
        self.batch_processor_task = asyncio.create_task(self._batch_processor_loop())
    
    async def stop(self):
        """Stop the batch processing loop"""
        self.running = False
        if self.batch_processor_task:
            self.batch_processor_task.cancel()
            try:
                await self.batch_processor_task
            except asyncio.CancelledError:
                pass
        await self.client.aclose()
    
    def add_request(self, sequence: str) -> asyncio.Future:
        """Add a new request and return a future for the result"""
        future = asyncio.Future()
        
        pending_req = PendingRequest(
            sequence=sequence,
            timestamp=time.time(),
            length=len(sequence),
            future=future
        )
        
        self.pending_requests[sequence] = pending_req
        self.request_queue.append(sequence)
        
        return future
    
    async def _batch_processor_loop(self):
        """Main loop that processes batches"""
        while self.running:
            try:
                # Process when we have requests
                if self.request_queue:
                    batch = self._create_optimal_batch()
                    if batch:
                        await self._process_batch(batch)
                    else:
                        await asyncio.sleep(0.001)
                else:
                    await asyncio.sleep(0.001)
                
            except Exception as e:
                print(f"Error in batch processor: {e}")
                await asyncio.sleep(0.01)
    
    def _create_optimal_batch(self) -> List[str]:
        """Create optimal batch by sorting requests by length"""
        if not self.request_queue:
            return []
        
        # Get all available requests with their lengths
        available_requests = []
        temp_queue = deque()
        
        # Collect all available requests
        while self.request_queue:
            sequence = self.request_queue.popleft()
            if sequence in self.pending_requests:
                req = self.pending_requests[sequence]
                available_requests.append((sequence, req.length))
            temp_queue.append(sequence)
        
        # Put back requests we didn't use
        while temp_queue:
            self.request_queue.appendleft(temp_queue.pop())
        
        if not available_requests:
            return []
        
        # Sort by length to minimize max_length in batches
        available_requests.sort(key=lambda x: x[1])
        
        # Take up to 5 shortest requests
        batch = []
        for sequence, _ in available_requests[:5]:
            batch.append(sequence)
            # Remove from queue
            if sequence in self.request_queue:
                self.request_queue.remove(sequence)
        
        return batch
    
    async def _process_batch(self, batch: List[str]):
        """Process a batch of requests"""
        if not batch:
            return
        
        try:
            # Prepare batch request
            sequences = []
            for sequence in batch:
                if sequence in self.pending_requests:
                    sequences.append(sequence)
            
            if not sequences:
                return
            
            classification_request = {"sequences": sequences}
            
            # Send batch request to classification server
            response = await self.client.post(CLASSIFICATION_SERVER_URL, json=classification_request)
            
            if response.status_code == 200:
                data = response.json()
                results = data["results"]
                
                # Distribute results to individual requests
                for i, sequence in enumerate(sequences):
                    if sequence in self.pending_requests:
                        req = self.pending_requests.pop(sequence)
                        if i < len(results) and not req.future.done():
                            req.future.set_result(results[i])
            
            elif response.status_code == 429:
                # Rate limited - put all requests back in queue
                for sequence in sequences:
                    if sequence in self.pending_requests:
                        self.request_queue.appendleft(sequence)
                await asyncio.sleep(0.01)
            
            else:
                # Other error - fail all requests in batch
                for sequence in sequences:
                    if sequence in self.pending_requests:
                        req = self.pending_requests.pop(sequence)
                        if not req.future.done():
                            req.future.set_exception(Exception(f"Server error: {response.status_code}"))
        
        except Exception as e:
            # Handle any other errors
            for sequence in batch:
                if sequence in self.pending_requests:
                    req = self.pending_requests.pop(sequence)
                    if not req.future.done():
                        req.future.set_exception(e)

# Global proxy instance
proxy = OptimalBatchingProxy()

@app.on_event("startup")
async def startup_event():
    """Start the proxy when the FastAPI app starts"""
    await proxy.start()

@app.on_event("shutdown")
async def shutdown_event():
    """Stop the proxy when the FastAPI app shuts down"""
    await proxy.stop()

@app.post("/proxy_classify")
async def proxy_classify(req: ProxyRequest):
    """
    Optimal batching proxy that:
    
    1. Sorts requests by length before batching
    2. Takes shortest requests first to minimize max_length
    3. Processes up to 5 requests per batch
    4. Minimizes latency through optimal length grouping
    """
    # Add request to proxy and get future for result
    future = proxy.add_request(req.sequence)
    
    # Wait for the result
    try:
        result = await future
        return ProxyResponse(result=result)
    except Exception as e:
        # Handle any errors that occurred during processing
        raise HTTPException(status_code=500, detail=str(e))
