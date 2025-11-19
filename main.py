import os
from io import BytesIO
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from zipfile import ZipFile, ZIP_DEFLATED
from datetime import datetime, timezone

from database import db, create_document
from bson import ObjectId
from bson.binary import Binary

app = FastAPI(title="Replix AI Backend", description="Generate Minecraft plugin projects as downloadable ZIPs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class CommandSpec(BaseModel):
    name: str = Field(..., description="Command name, e.g. hello")
    usage: Optional[str] = Field(None, description="Usage string, e.g. /hello")
    description: Optional[str] = Field(None, description="Short description")

class GenerateRequest(BaseModel):
    plugin_name: str = Field(..., description="Human friendly plugin name")
    package_name: str = Field(..., description="Java package, e.g. com.example.myplugin")
    description: str = Field("", description="Plugin description")
    api: str = Field("spigot", description="Target API (spigot/paper/bukkit)")
    commands: List[CommandSpec] = Field(default_factory=list)


def _plugin_yml(name: str, main_class: str, description: str, commands: List[CommandSpec]) -> str:
    lines = [
        f"name: {name}",
        "version: 1.0.0",
        "api-version: '1.20'",
        f"main: {main_class}",
        f"description: {description if description else name}",
    ]
    if commands:
        lines.append("commands:")
        for cmd in commands:
            cname = cmd.name.strip()
            if not cname:
                continue
            lines.append(f"  {cname}:")
            if cmd.description:
                lines.append(f"    description: {cmd.description}")
            lines.append(f"    usage: /{cname}")
            lines.append("    permission: replix." + cname)
            lines.append("    permission-message: You don't have permission to use this command.")
    return "\n".join(lines) + "\n"


def _main_java(package_name: str, class_name: str, description: str, commands: List[CommandSpec]) -> str:
    register_cmds = "\n".join([
        f"        this.getCommand(\"{c.name}\").setExecutor(new {c.name.capitalize()}Command());" for c in commands if c.name
    ])
    imports_cmds = "\n".join([
        f"import {package_name}.commands.{c.name.capitalize()}Command;" for c in commands if c.name
    ])
    return f"""
package {package_name};

import org.bukkit.plugin.java.JavaPlugin;
{imports_cmds}

public class {class_name} extends JavaPlugin {{

    @Override
    public void onEnable() {{
        getLogger().info("{description if description else class_name} enabled!");
{register_cmds}
    }}

    @Override
    public void onDisable() {{
        getLogger().info("{class_name} disabled!");
    }}
}}
""".strip() + "\n"


def _command_java(package_name: str, cmd: CommandSpec) -> str:
    class_name = f"{cmd.name.capitalize()}Command"
    message = cmd.description or f"/{cmd.name} executed!"
    return f"""
package {package_name}.commands;

import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;

public class {class_name} implements CommandExecutor {{

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {{
        sender.sendMessage("{message}");
        return true;
    }}
}}
""".strip() + "\n"


def build_plugin_zip(req: GenerateRequest) -> bytes:
    pkg = req.package_name.strip()
    if not pkg or "." not in pkg:
        raise HTTPException(status_code=400, detail="package_name must be a valid Java package like com.example.plugin")

    main_class_name = "Main"
    full_main_class = f"{pkg}.{main_class_name}"

    buf = BytesIO()
    with ZipFile(buf, mode="w", compression=ZIP_DEFLATED) as z:
        # plugin.yml
        z.writestr("plugin.yml", _plugin_yml(req.plugin_name, full_main_class, req.description, req.commands))

        # src structure
        java_path = "src/main/java/" + pkg.replace(".", "/") + "/"
        z.writestr(java_path + main_class_name + ".java", _main_java(pkg, main_class_name, req.description, req.commands))

        # command executors
        if req.commands:
            commands_pkg_path = java_path + "commands/"
            for cmd in req.commands:
                cname = (cmd.name or "").strip()
                if not cname:
                    continue
                z.writestr(commands_pkg_path + cname.capitalize() + "Command.java", _command_java(pkg, cmd))

        # pom.xml minimal for Maven
        pom = f"""
<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>{pkg}</groupId>
  <artifactId>{req.plugin_name.lower().replace(' ', '-')}</artifactId>
  <version>1.0.0</version>
  <name>{req.plugin_name}</name>
  <description>{req.description}</description>
  <build>
    <sourceDirectory>src/main/java</sourceDirectory>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>3.11.0</version>
        <configuration>
          <source>17</source>
          <target>17</target>
        </configuration>
      </plugin>
    </plugins>
  </build>
  <dependencies>
    <dependency>
      <groupId>org.spigotmc</groupId>
      <artifactId>spigot-api</artifactId>
      <version>1.20.1-R0.1-SNAPSHOT</version>
      <scope>provided</scope>
    </dependency>
  </dependencies>
  <repositories>
    <repository>
      <id>spigot-repo</id>
      <url>https://hub.spigotmc.org/nexus/content/repositories/snapshots/</url>
    </repository>
    <repository>
      <id>sonatype</id>
      <url>https://oss.sonatype.org/content/groups/public/</url>
    </repository>
  </repositories>
</project>
""".strip() + "\n"
        z.writestr("pom.xml", pom)

        # .gitignore
        z.writestr(".gitignore", ".idea\n*.iml\n*.class\n/target\n")

    return buf.getvalue()


@app.get("/")
def read_root():
    return {"message": "Replix AI Backend is running"}


@app.post("/api/generate")
def generate_plugin(req: GenerateRequest):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")

    zip_bytes = build_plugin_zip(req)
    size = len(zip_bytes)

    doc = {
        "plugin_name": req.plugin_name,
        "package_name": req.package_name,
        "description": req.description,
        "api": req.api,
        "commands": [c.model_dump() for c in req.commands],
        "files": [
            "plugin.yml",
            f"src/main/java/{req.package_name.replace('.', '/')}/Main.java",
        ] + ([f"src/main/java/{req.package_name.replace('.', '/')}/commands/{c.name.capitalize()}Command.java" for c in req.commands if c.name] if req.commands else []),
        "archive_size": size,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "zip": Binary(zip_bytes),
    }

    result = db["generation"].insert_one(doc)
    gen_id = str(result.inserted_id)

    return JSONResponse({
        "id": gen_id,
        "archive_size": size,
        "download_url": f"/api/download/{gen_id}",
        "message": "Plugin generated successfully"
    })


@app.get("/api/download/{gen_id}")
def download_plugin(gen_id: str):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    try:
        oid = ObjectId(gen_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")

    doc = db["generation"].find_one({"_id": oid})
    if not doc or not doc.get("zip"):
        raise HTTPException(status_code=404, detail="Archive not found")

    data: bytes = bytes(doc["zip"])  # Binary -> bytes
    filename = f"{doc.get('plugin_name','plugin').lower().replace(' ', '-')}.zip"
    return StreamingResponse(BytesIO(data), media_type="application/zip", headers={
        "Content-Disposition": f"attachment; filename={filename}"
    })


@app.get("/api/history")
def history(limit: int = 20):
    if db is None:
        raise HTTPException(status_code=500, detail="Database not available")
    items = db["generation"].find({}, {"zip": 0}).sort("created_at", -1).limit(limit)
    result = []
    for it in items:
        it["id"] = str(it.pop("_id"))
        result.append(it)
    return {"items": result}


@app.get("/test")
def test_database():
    response = {
        "backend": "✅ Running",
        "database": "❌ Not Available",
        "database_url": None,
        "database_name": None,
        "connection_status": "Not Connected",
        "collections": []
    }
    try:
        if db is not None:
            response["database"] = "✅ Available"
            response["database_url"] = "✅ Configured"
            response["database_name"] = db.name if hasattr(db, 'name') else "✅ Connected"
            response["connection_status"] = "Connected"
            try:
                collections = db.list_collection_names()
                response["collections"] = collections[:10]
                response["database"] = "✅ Connected & Working"
            except Exception as e:
                response["database"] = f"⚠️  Connected but Error: {str(e)[:50]}"
        else:
            response["database"] = "⚠️  Available but not initialized"
    except Exception as e:
        response["database"] = f"❌ Error: {str(e)[:50]}"
    import os as _os
    response["database_url"] = "✅ Set" if _os.getenv("DATABASE_URL") else "❌ Not Set"
    response["database_name"] = "✅ Set" if _os.getenv("DATABASE_NAME") else "❌ Not Set"
    return response


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
