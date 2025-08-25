**TwinVine**

**Beta in testing** .

![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA.png)


TwinVine is the home of TWO packages  [Vinefeeder](https://github.com/vinefeeder/TwinVine/blob/main/packages/vinefeeder/src/vinefeeder/README.md)
and [Envied](https://github.com/vinefeeder/TwinVine/blob/main/packages/envied/README.md)

TwinVine is the easy way to handle your download tasks. 
* When you have an exact program url - just use envied as a command line call.
* When you only have the program name - just start with a search in vinefeeder.
* When you dont know what you want; use the browse function.
* Browse media categories like Film, Drama or Sport, for a selected service.
* Batch Mode: select multiple downloads from various services and download all together.
 


**usage**
```
uv run envied dl (options) (service) (program url)

uv run vinfeeder  - to open the interactive GUI for search, browse, etc.
```
**Installation**

Uv is the package manager and loads both VineFeeder and Envied together.  Envied runs independenly or may be called by Vinefeeder.

If you do not alrealy have uv as a python package intall it first using pip
```
pip install uv

or

python3 -m pip install uv
```

Then install TwinVine the following installs the latest version directly from the GitHub repository:

```shell
git clone https://github.com/vinefeeder/TwinVine.git
cd TwinVine
uv clean
uv.lock
uv sync
uv run vinefeeder --help or
uv run envied --help
```
**Configuration**

Run this line inside the VineFeeder folder:
```
cp packages/envied/src/envied/envied-working-example.yaml packages/envied/src/envied/envied.yaml 
```
This ensures you have your own working copy of envied.yaml. It may be edited and will not be over-written during any updates.  
  
That's it; uv run vinfeeder to get started!

**Services**

Vinefeeder currently has 8 services for which search, browse and list-select are available  
  
  ALL4  BBC  ITVX  MY5  STV  TPTV  TVNZ  U 
  
Envied has   

ALL4  AUBC  CBS  DSCP  iP   MAX   MY5   NF   PCOK   ROKU  SPOT  TPTV  TVNZ  YTBE
ARD   CBC   CTV  DSNP  ITV  MTSP  NBLA  NRK  PLUTO  RTE   STV   TUBI  UKTV  ZDF
Not all have been tested

Images
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder2.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder4.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder5.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder6.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder7.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder8.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder9.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder10.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder11.png)
    



