from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH

root=Path(__file__).resolve().parents[1]
out=root/"Distributed_Object_Storage_Architecture_Report.docx"
d=Document()
s=d.sections[0]
s.top_margin=Inches(.7); s.bottom_margin=Inches(.7); s.left_margin=Inches(.85); s.right_margin=Inches(.85)
d.styles["Normal"].font.name="Aptos"; d.styles["Normal"].font.size=Pt(10)
d.styles["Title"].font.name="Aptos Display"; d.styles["Title"].font.size=Pt(25); d.styles["Title"].font.color.rgb=RGBColor(31,78,121)
for name in ("Heading 1","Heading 2"):
    d.styles[name].font.name="Aptos Display"; d.styles[name].font.color.rgb=RGBColor(31,78,121)

def bullet(title, text):
    p=d.add_paragraph(style="List Bullet")
    p.add_run(title+" ").bold=True
    p.add_run(text)

d.add_paragraph("Distributed Object Storage System",style="Title")
p=d.add_paragraph(); p.alignment=WD_ALIGN_PARAGRAPH.LEFT
r=p.add_run("Architecture Report"); r.bold=True; r.font.size=Pt(15); r.font.color.rgb=RGBColor(89,89,89)
d.add_paragraph("Course project | 29 October 2026")
d.add_heading("Executive overview",1)
d.add_paragraph("The project uses a layered architecture for distributed object storage. A React console and API clients send requests through Nginx to FastAPI gateway replicas. The gateway provides authentication and object APIs. A metadata service chunks data, selects storage targets, enforces durability policies, and records manifests and placements in PostgreSQL. Storage nodes persist hash-addressed chunks or coded fragments. RabbitMQ distributes maintenance jobs, Elasticsearch supports metadata search, and Prometheus/Grafana provide monitoring.")
d.add_paragraph("The local Compose topology is implemented as configuration. Kubernetes files provide a storage StatefulSet and a cloud-facing gateway autoscaling template. The cloud files are not a live cloud deployment, and local PostgreSQL is still a single instance.")

d.add_heading("Logical component view",1)
diagram=[
"Browser / API client",
"       |",
"       v",
"Nginx reverse proxy :8080",
"       |",
"       +---- Gateway replica A ----+",
"       +---- Gateway replica B ----+---- Metadata API ---- PostgreSQL",
"                    |                         |",
"React UI :5173 -----+                         +---- Storage node set",
"                                              ^       |",
"                                              |       | heartbeats",
"                                      RabbitMQ queues <-+",
"                                              |",
"                                         Job workers",
"",
"Gateway / metadata / storage metrics ---> Prometheus ---> Grafana",
"Gateway metadata search ---------------> Elasticsearch"
]
for line in diagram:
    p=d.add_paragraph()
    p.paragraph_format.space_after=Pt(0)
    r=p.add_run(line); r.font.name="Consolas"; r.font.size=Pt(8.5)
d.add_paragraph("This diagram expresses configured relationships. It does not claim that all replicas or external dependencies are currently healthy; verify those against the running stack.")

d.add_heading("Component responsibilities",1)
for title,text in [
("React and Vite console.","Provides sign-in, bucket and object browsing, uploads, downloads, deletes, search, and cluster visibility. It accesses the system through the gateway."),
("Nginx reverse proxy.","Provides a single HTTP entry point and forwards requests to gateway replicas. Compose maps it to localhost port 8080."),
("FastAPI gateway.","Validates JWTs, applies request limits, exposes bucket/object APIs, streams data, handles byte ranges, queries search, and presents cluster status."),
("Metadata API.","Splits objects into fixed or content-defined chunks, assigns SHA-256 content addresses, maps chunks to nodes using a virtual-node consistent-hash ring, and coordinates durability, repair, scrub, and rebalance."),
("PostgreSQL.","Stores manifests, versions, policies, reference counts, node heartbeats, fragment placements, repair jobs, and reliability counters. It is a single local instance."),
("Storage nodes.","Write chunks to local persistent storage, reject content whose digest does not match its address, verify stored bytes before reads, and report health/capacity heartbeats."),
("RabbitMQ and job workers.","Carry and process repair, rebalancing, and garbage-collection jobs. The worker currently polls RabbitMQ through its management API."),
("Elasticsearch.","Indexes object metadata to support prefix, size, date, and tag searches. PostgreSQL remains the metadata source of record."),
("Prometheus and Grafana.","Prometheus scrapes gateway latency/request metrics, node disk/chunk gauges, and metadata degraded-object/repair metrics. Grafana visualizes these series.")]:
    bullet(title,text)

d.add_heading("Data and request flows",1)
d.add_heading("Upload",2)
d.add_paragraph("A client sends an authenticated upload through Nginx to a gateway. The gateway streams the body to metadata. Metadata chunks the stream, hashes each logical chunk, obtains the bucket policy, and selects live storage targets. It writes replicas or Reed-Solomon fragments to storage nodes and commits the ordered object manifest, version, tags, references, and placement metadata in PostgreSQL. The gateway indexes searchable metadata in Elasticsearch.")
d.add_heading("Download",2)
d.add_paragraph("The gateway requests the object or requested byte range from metadata. Metadata obtains the configured read quorum or enough independent erasure fragments, verifies fragment hashes, reconstructs encoded content as needed, and validates the logical chunk digest. The gateway streams the response to the caller.")
d.add_heading("Deduplication and deletion",2)
d.add_paragraph("The SHA-256 address lets identical content share storage. PostgreSQL reference counts track which object manifests use a chunk. Deleting an object decrements references; the final reference removal allows physical data and placement records to be cleaned up.")
d.add_heading("Failure recovery and rebalancing",2)
d.add_paragraph("Storage nodes send periodic heartbeats. Metadata marks expired or failed placements, creates repair jobs, and queues work to RabbitMQ. Scrubbing rereads fragments and flags hash mismatches. A newly registered or recovered node queues a rebalance job so workers can compare placements with the current ring and relocate misplaced fragments.")

d.add_heading("Metadata model",1)
for title,text in [
("Object manifest.","Bucket, key, size, version, chunking mode, ordered chunk descriptors, creation date, tags, and owner."),
("Chunk reference.","Logical content digest, size, physical node reference, and reference count."),
("Fragment placement.","Logical digest, policy ID, fragment index, fragment digest, node ID, byte size, durability configuration, and health status."),
("Node status.","Node ID, advertised address, free bytes, chunk count, last heartbeat, and health state."),
("Reliability records.","Repair reason/status/timing/traffic plus cumulative repair and scrub counters."),
("Search document.","Indexed object key/bucket/owner, size/date, and tags in Elasticsearch.")]:
    bullet(title,text)

d.add_heading("Deployment architecture",1)
d.add_heading("Local Compose",2)
d.add_paragraph("compose.yaml contains common development dependencies and monitoring. compose.storage.yaml defines PostgreSQL, Elasticsearch, metadata, storage nodes, gateway, Nginx, worker, and UI. Local ports include gateway 8080, metadata 8100, PostgreSQL 5433, Elasticsearch 9200, storage nodes 8201 onward, UI 5173, Prometheus 9090, Grafana 3000, and RabbitMQ management 15672.")
d.add_heading("Kubernetes and cloud template",2)
d.add_paragraph("The Kubernetes resources describe a four-replica storage StatefulSet with persistent volume claims and health probes, a gateway Deployment, a cloud LoadBalancer Service, and a CPU-based HorizontalPodAutoscaler. A real deployment requires images in a registry, secrets, a working cloud cluster, and reachable metadata, RabbitMQ, and PostgreSQL services. These resources have not been deployed to AWS, Google Cloud, or Azure.")

d.add_heading("Architecture constraints",1)
for text in [
"Local metadata depends on one PostgreSQL instance, which is a single point of failure.",
"Gateway rate limiting is process-local and is not shared across replicas.",
"RabbitMQ management polling acknowledges a job when dequeued; a worker failure afterward can lose the job.",
"Kubernetes pods need metadata and dependencies inside the Kubernetes network; Compose service names do not automatically resolve there.",
"The cloud manifest is a template and does not provision a provider cluster or managed database.",
"The latest attempted local Docker build reported an exec format error, so the full live topology requires a successful rebuild and runtime verification."]:
    d.add_paragraph(text,style="List Bullet")

d.add_heading("Repository map",1)
for title,text in [
("gateway_service/.","Gateway API and service Dockerfile."),
("metadata_service/.","Metadata, chunking, ring, durability, and repair logic."),
("storage_node/.","Storage-node API and Dockerfile."),
("frontend/.","React/Vite application, lockfile, and UI Dockerfile."),
("scripts/ and tests/.","Verification programs, benchmark generator, worker, report helpers, and pytest coverage."),
("compose files and infra/.","Local service definitions, Nginx, Prometheus, and Grafana configuration."),
("deploy/kubernetes/.","Storage StatefulSet and cloud gateway autoscaling template."),
 ("reports/ and docs/.","Saved verification outputs, task guides, and architecture documentation.")]:
    bullet(title,text)
d.add_paragraph("Use README.md and docs/<task>/README.md for task-level startup and test instructions.")
d.core_properties.title="Distributed Object Storage Architecture Report"
d.core_properties.subject="Components, interactions, metadata model, and deployment topology"
d.core_properties.author="Course Project Team"
d.save(out)
print(out)
