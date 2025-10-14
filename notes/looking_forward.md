- use agentic framework for js with tool calling (doesn't need mcps)
- wrap current functionality (mappings) as tools
- make specific tools with less noise (like not showing all the json of an entire workflow instead showing a zoomed -out diagram)
- tool refinement (making the output of tools more concise)
- query tool for querying the workflow (like ast level querying but as an inspector within js) 

Things to remember about node representation in query reults
    - name, id, parameters and values, xy position, (connections [inputs and outputs])