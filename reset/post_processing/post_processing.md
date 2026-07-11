# Post Processing.

With a mega program like envied it is sometimes difficult to make internal program changes to reflect personal needs.

Using a post-processor on the downloaded results may provide a solution.

I have found two unmet needs:

* Extract srt subtitles 
EDIT  There is now an envied option
- mux: Embed subtitles in MKV container only (default)
- sidecar: Save subtitles as separate files only
- both: Embed in MKV AND save as sidecar files
see the envied-working example.yaml and edit for your preference.
* Convert mkv output to mp4 (or any other video container format, with code adjustment)

Here are two scripts which operate from a root folder (downloads - for instance), and operate on all the mkv files found within. The original files are left in place - for you to remove as necessary.

## Extract srt subtitles

Use 
* uv run envied dl -S my5 https://www.channel5.com/show/taggart/season-1/killer    

-S tells envied to download subtitles only. However it produces an mks file as a result.   

Run  'python extract_mks_subs.py' in the root folder with your mks downloaded files. There are options: --dry-run will allow checking all is well before extraction.

If you start with a full mkv container - video, audio and subtitles tracks - and wish to extract subtitles, then they will no longer be held in track 0 of an mks file - the program defaults. So use the --track parameter and set it to the third track of 0,1,2. And the full container extension is mkv -the script needs the default settings over-riding with:  

  * 'python extract_mks_subs.py --track 2 --ext mkv '.    


## Convert to mp4

Run  
* 'python mkv_to_mp4.py'  

in the root folder with mkv files. 

The option: --dry-run will allow checking all is well before converion.  
  
Conversion to other formats is complex and not suited to this simple routine as the audio/video codecs would each need need re-coding to suit the required output.