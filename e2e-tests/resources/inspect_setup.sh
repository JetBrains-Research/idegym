# Write the inspection profile to the project's .idea directory so that
# inspect.sh can pick it up via -profile.
# The placeholder below is substituted at test load time with the XML content
# from the corresponding *_inspect_profile.xml resource file.
mkdir -p /root/work/.idea/inspectionProfiles
printf '%s\n' '{profile}' > /root/work/.idea/inspectionProfiles/Default.xml
echo "Inspection profile written"
