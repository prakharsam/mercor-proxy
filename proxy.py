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
    title="Simple Optimal Proxy",
    description="Simple but effective batching that processes immediately"
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

class SimpleOptimalProxy:
    """Simple optimal proxy that processes batches immediately"""
    
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
        """Main loop that processes batches immediately"""
        while self.running:
            try:
                # Process immediately when we have requests
                if self.request_queue:
                    batch = self._create_simple_batch()
                    if batch:
                        await self._process_batch(batch)
                    else:
                        await asyncio.sleep(0.0001)  # 0.1ms - very fast
                else:
                    await asyncio.sleep(0.0001)  # 0.1ms
                
            except Exception as e:
                print(f"Error in batch processor: {e}")
                await asyncio.sleep(0.001)
    
    def _create_simple_batch(self) -> List[str]:
        """Create simple batch - take first 5 requests"""
        if not self.request_queue:
            return []
        
        # Take up to 5 requests from the queue
        batch = []
        for _ in range(5):
            if self.request_queue:
                sequence = self.request_queue.popleft()
                if sequence in self.pending_requests:
                    batch.append(sequence)
            else:
                break
        
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
                await asyncio.sleep(0.001)  # Minimal backoff
            
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
proxy = SimpleOptimalProxy()

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
    Simple optimal proxy that:
    
    1. Takes first 5 requests from queue
    2. Processes batches immediately
    3. Uses very fast polling (0.1ms)
    4. Focuses on throughput over perfect optimization
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
