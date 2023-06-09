import collections.abc
import datetime
import getopt
import json
import jsonschema
import os.path
import pprint
import requests
import settings
import sys
import time
import traceback
import urllib.parse
import csv
import re
from cvss import CVSS2, CVSS3
from dateutil.parser import parse as dateParse
from langcodes import Language
from progress.spinner import Spinner
from numbers import Number
from requests.utils import requote_uri

JSONValidator = None
JSONValidatorPublished = None

v5SchemaPath = settings.v5schemafile
v5SchemaPath_published = settings.v5schemafile_published

BASE_HEADERS = {
    'CVE-API-KEY': settings.AWG_USER_KEY,
    'CVE-API-ORG': settings.AWG_USER_CNA_NAME,
    'CVE-API-USER': settings.AWG_USER_NAME
}

keys_used = {}
extra_keys = {}
defaulted_users = {}
all_users = {}
all_orgs = {}
user_errors = {}
states_processed = []
scoring_other = {}
invalid_impact_versions = []
requester_map = {}
reference_tag_map = {}
cveHistory = {}
ValidationFailures = {}
cvssErrorList = []
minShortName = 100
maxShortName = 0
maxTitle = 0
v5MaxTitleLength = 256  # update to pull from schema file
maxV5VersionLength = 1024  # update to pull from schema file
maxV5ProductLength = 2048  # update to pull from schema file
historyDateTimeFormat = '%Y-%m-%d %H:%M:%S.%f'
IDRWaitTime = 0.00
IDRCollection = {}  # cve-id arrays indexed by id

def main(argv):
    inputfile = ''
    inputdir = ''
    outputpath = ''
    
    
    if "-test" in argv:
        print("Testing Connection to CVE Services")
        print( str(testCVEServicesConnection()) )

        getRequesterMap()
        global requester_map
        print(json.dumps(requester_map, indent=2))
        sys.exit(0)
    
    try:
        opts, args = getopt.getopt(argv, "hi:o:d:", ["ifile=","opath=","idir="])
    except getopt.GetopError:
        print ('USAGE python cve4to5up.py -i <inputfile>|-d <inputdirectory> -o <outputpath>')
        sys.exit(2)
        
    for opt, arg in opts:
        if opt == '-h':
            sys.exit()
        elif opt in ("-i", "--ifile"):
            inputfile = arg
        elif opt in ("-d", "--idir"):
            inputdir = arg
        elif opt in ("-o", "--opath"):
            outputpath = arg

    # Load CVE Record change history timestamps
    print("Loading History Dates - Start")
    sTime = time.perf_counter()
    global cveHistory
    try:
        with open("cve_record_dates.json") as CVEH:
            for ch in json.load(CVEH):
                if not ch["cve_identifier"] in cveHistory:
                    cveHistory[ch["cve_identifier"]] = []
                cveHistory[ch["cve_identifier"]].append(ch)
    except Exception as ex:
        print( str(ex))
        print("Failed to load CVE Record History Dates")
        exit(1)

    hTime = time.perf_counter() - sTime                
    print("Loading History Dates - Finished in: " + '{0:2f}'.format(hTime))
       
    if inputfile and outputpath:
        CVE_Convert(inputfile, outputpath)
    elif inputdir and outputpath:
        # loop all *.JSON in input directory
        print('START processing directory: ', inputdir)
        spinner = Spinner('Converting ')
        problemfiles = {}
        CVECount = 0
        spinnerCount = 250
        previousTime = time.perf_counter()
        startTime = previousTime
        for subdir, dirs, files in os.walk(inputdir):
            for f in files:
                filepath = subdir + os.sep + f
                opath = ''
                if filepath.lower().endswith(".json"):
                    cStart = time.perf_counter()
                    # strip input path from subdir
                    dtree = subdir
                    if dtree.startswith(inputdir):
                        dtree = dtree.replace( inputdir, '')
                    opath = outputpath + dtree
                    try:
                        CVE_Convert(filepath, opath)    
                    except:
                        problemfiles[filepath] = "" + str(sys.exc_info()[0]) + " -- " + str(sys.exc_info()[1]) + " -- "
                    cDuration = time.perf_counter() - cStart
                    # print("Convert time for " + inputfile + " took " + '{0:.4f}'.format(cDuration))

                CVECount += 1    
                # if CVECount % 100 == 0: spinner.next()
                if CVECount % 10 == 0: 
                    newTime = time.perf_counter()
                    setTime = newTime - previousTime
                    print("Processed " + str(spinnerCount) + " in " + '{0:.2f}'.format(setTime) + " : total processed = " + str(CVECount))
                    previousTime = newTime
                    spinner.next()

        convertingTime = time.perf_counter() - startTime
        print('FINISHED processing directory', inputdir)
        print('Processin time was: ' + str(convertingTime) + ' seconds')
        print('Time waited for IDR info: ' + str(IDRWaitTime))
        print('')
        print('UP CONVERT JOB REPORT')
        print(str(len(ValidationFailures)) + " upconverter records failed to validate")

        print('')
        print("Shortname: min="+str(minShortName)+" -- max="+str(maxShortName))
        print("Title: max="+str(maxTitle))
        print('')
        

        if problemfiles:
            print("JSON files that failed to convert: " + str(len(problemfiles)) + " of " + str(CVECount))
        else:
            print(str(CVECount) + " JSON files converted.")
        print('')
        
        print('cvss errors encounters: ' + str(len(cvssErrorList)))
        print('these are counted in the failed to validate number')
        print('these are from cvss library exceptions, and indicate the provide vectorString')
        print('from the v4 record is not parsable even after stripping spaces and prefixing versions')
        print('')
        
        if extra_keys:
            for e in extra_keys:                
                print("Extra keys encountered")
                print( e )
                for ek in extra_keys[e]:
                    print("    ", ek, " - used in", len(extra_keys[e][ek]), " records.")
                    
        print('')
        print('Users not found for conversion to UUID --- ' + str(len(defaulted_users)))
        if ( len(defaulted_users) < 1 ):
            print(' --- all users seen were convertable')
        else:
        
            defaulted_record_count = 0
            for du in defaulted_users:
                print(du + " --- " + str(len(defaulted_users[du])))
                defaulted_record_count += len(defaulted_users[du])
            print("total records re-assigned to default = " + str(defaulted_record_count))
        print('')
        
        print('')
        print('User errors encountered (in multiple orgs) --- ' + str(len(user_errors)))
        if ( len(user_errors) < 1 ):
            print('No user errors encountered')
        else:
            for ue in user_errors:
                print(ue + " --- " + str(len(user_errors[ue])+1))
        print('')
                
        '''
        print('')
        print('Saw v4 STATEs')
        for s in states_processed:
            print(s)
        print('')
        ''' 
           
        print('')
        print("Unsupported IMPACT version values found  --- " + str(len(invalid_impact_versions)))
        for iiv in invalid_impact_versions:
            print(" --- "+iiv+" : "+invalid_impact_versions[iiv]["count"])
        print('')
        if scoring_other:
            print("IMPACT Scoring data remapped into 'other' --- " +str(len(scoring_other)))
            print('')
    
        print('')
        print('----- DETAILED RESULTS -----')

        print('')
        if problemfiles:
            print('=== SECTION -- failed to convert errors ===')        
            print("JSON files that failed to convert (" + str(len(problemfiles)) + "): ")
            for fname in problemfiles:
                print(fname)
                print("    ", problemfiles[fname])
        else:
            print("No JSON files failed to produce to a new file.")
 

        if extra_keys and False:
            print('')
            print('')
            print('=== SECTION -- extra keys ===')
            print("Detailed Extra keys encountered")
            for e in extra_keys:                
                print( e )
                pp = pprint.PrettyPrinter(indent=4)
                pp.pprint(extra_keys[e])

        if scoring_other and False:
            print('')
            print('')
            print('=== SECTION -- other scoring values ===')
            print("Scoring data remapped into 'other' --- " +str(len(scoring_other)))
            for e in scoring_other:                
                print( e )
                pp = pprint.PrettyPrinter(indent=4)
                pp.pprint(scoring_other[e])
            print('')
            print('')
            print('')

        if len(cvssErrorList) > 0 and False:
            print('')
            print('')
            print('=== SECTION -- cvss errors ===')
            print('cvss errors encountered: ' + str(len(cvssErrorList)))
            pp = pprint.PrettyPrinter(indent=4)
            pp.pprint(cvssErrorList)
            print('')
            print('')



        if len(ValidationFailures) > 0:
            print('')
            print('')
            print('=== SECTION -- validation errors ===')
            print('records with validation errors encountered: ' + str(len(ValidationFailures)))
            pp = pprint.PrettyPrinter(indent=4)
            pp.pprint(ValidationFailures)
            print('')
            print('')

        print('')
        print('')
        print('=== SECTION -- user errors ===')
        print('Detailed users not found for conversion to UUID, and resassign to the default(secretariat)')
        if ( len(defaulted_users) < 1 ):
            print('all users seen were convertable')
        else:
            for du in defaulted_users:
                print(du)
                idList = ""
                idList = ", ".join([str(did) for did in defaulted_users[du]])
                print(idList)
                print("-----")
                # for did in defaulted_users[du]:
                #     print (" --- " + did)
        print('')

        print('Done')
    else:
        print('incorrect input parameters')
        print('USAGE python cve4to5up.py -i <inputfile>|-d <inputdirectory> -o <outputpath>')    
        
    sys.exit(0)
            
def convert_VA(vd):
    if not "version_affected" in vd and "affected" in vd:
        vd["version_affected"] = vd["affected"]
    if "version_affected" in vd and re.match("[!?<>=]",vd["version_affected"]):
        va = vd["version_affected"]
        vstatus = "affected"
        if "!" in va:
            vstatus = "unaffected"
            va = va.replace("!", "")
        elif "?" in va:
            vstatus = "unknown"
            va = va.replace("?", "")
        if len(va) == 0:
            va = "="
        return ([vstatus, va])
    else:
        return (["affected", "="])

def eq_version(vd, status):
    ver = vd["version_name"] + ' ' + vd["version_value"]
    if vd["version_value"].startswith(vd["version_name"]):
        ver = vd["version_value"]
    return({
        "version": ver,
        "status": status
    })

def l_version(vd, status):
    return({
        "version": vd["version_name"],
        "status": status,
        "lessThan": vd["version_value"],
        "versionType": "custom"
    })

def le_version(vd, status):
    return({
        "version": vd["version_name"],
        "status": status,
        "lessThanOrEqual": vd["version_value"],
        "versionType": "custom"
    })

def negate(status):
    if status == 'affected':
        return "unaffected"
    elif status == "unaffected":
        return "affected"
    else:
        return status

def nonEmpty(v):
    if 'version' in v and v["version"] == "":
        v["version"] = "unspecified"
    return v

def redux_CVSS(c, initvector):
    tm = ['exploitCodeMaturity', 'exploitability', 'remediationLevel', 'reportConfidence', 'temporalScore', 'temporalSeverity']
    em = ["collateralDamagePotential", "targetDistribution", "confidentialityRequirement", "integrityRequirement", "availabilityRequirement", "environmentalScore",
        "modifiedAttackVector","modifiedAttackComplexity","modifiedPrivilegesRequired","modifiedUserInteraction","modifiedScope",
        "modifiedConfidentialityImpact","modifiedIntegrityImpact","modifiedAvailabilityImpact","environmentalSeverity"]
    if(not re.search('/(E|RL|PC|RC):[A-Z]', initvector)):
        for m in tm:
            if m in c:
                del c[m]
    if(not re.search('/(CDP|TD|M[A-Z]{1,2}|[CIA]R):', initvector)):
        for m in em:
            if m in c:
                del c[m]
    return c

def IBM_score(cvss):
    vec = "CVSS:3.0"
    del cvss["BM"]["SCORE"]
    for a in ["BM", "TM"]:
        if a in cvss:
            for k in cvss[a]:
                vec = vec + "/" + k + ":" + cvss[a][k]
    return vec

def CVE_Convert(inputfile, outputpath):  
    # print("input - ", inputfile, " :: output - ", outputpath)
    global keys_used
    global extra_keys
    global states_processed
    # global all_users
    global all_orgs
    global scoring_other
    global invalid_impact_versions
    global requester_map
    global reference_tag_map
    global minShortName
    global maxShortName
    global maxTitle
    global v5MaxTitleLength
    global maxV5VersionLength
    global maxV5ProductLength
       
    if len(requester_map) < 1:
        getRequesterMap()

    if len(reference_tag_map) < 1:
        getReferenceTagMap()
        
    
    with open(inputfile) as json_file:
        writeout = False
        data = json.load(json_file)
        jout = {}
        # keys_used["data_format"] = {}
        jout["dataType"] = "CVE_RECORD"
        # keys_used["data_type"] = {}
        jout["dataVersion"] = "5.0"
        # keys_used["data_version"] = {}
        
        converter_errors = {}
        
        # up convert meta
        o_meta = {}
        try:
            if "CVE_data_meta" in data and "STATE" in data["CVE_data_meta"]:
                i_meta = data["CVE_data_meta"]
                if i_meta["STATE"] not in keys_used: keys_used[i_meta["STATE"]] = {}
                keys_used[i_meta["STATE"]]["CVE_data_meta"] = {}


                if "STATE" in i_meta:
                    if i_meta["STATE"] == 'RESERVED':
                        o_meta['state'] = 'RESERVED'
                    elif i_meta["STATE"] == 'PUBLIC':
                        o_meta['state'] = 'PUBLISHED'
                    elif i_meta["STATE"] == 'REJECT':
                        o_meta['state'] = 'REJECTED'
                    else:
                        o_meta['state'] = i_meta["STATE"]
                    if o_meta["state"] not in states_processed: states_processed.append(o_meta["state"])

                if "ID" in i_meta: 
                    o_meta["cveId"] = i_meta["ID"]

                recordHistory = []
                if o_meta["cveId"] in cveHistory:
                    recordHisotry = cveHistory[o_meta["cveId"]].copy()

                o_meta["assignerOrgId"] = "Not found"
                o_meta["assignerShortName"] = "Not found"
                if i_meta["STATE"] != 'RESERVED':
                    pTime = time.perf_counter()
                    
                    recData = getIDRInfo( o_meta["cveId"] )   
                    
                    setTime = time.perf_counter() - pTime
                    # print("getIDRInfo took:"  + str(setTime))
                    global IDRWaitTime
                    IDRWaitTime = IDRWaitTime + setTime
                    
                    if recData and "owning_cna" in recData:
                        org_uuid = recData["owning_cna"]
                        org_short_name = getOrgShortName(org_uuid)
                        # org_short_name = recData["owning_cna"]
                        # org_uuid = getOrgUUID(org_short_name)

                        o_meta["assignerOrgId"] = org_uuid
                        if org_short_name:
                            o_meta["assignerShortName"] = org_short_name
                    else:
                        print("Record with data issue: " + o_meta["cveId"])
                        raise Exception("ERROR - no CNA for record ID - " + o_meta["cveId"])
                    
                if "DATE_PUBLIC" in i_meta and i_meta["DATE_PUBLIC"] != "": 
                    o_meta["datePublished"] = i_meta["DATE_PUBLIC"]
                    try:
                        if not isinstance(o_meta["datePublished"], datetime.datetime):
                            o_meta["datePublished"] = str(datetime.datetime.combine(dateParse(o_meta["datePublished"]).date(), datetime.datetime.min.time()).isoformat())

                        keys_used["PUBLIC"]["DATE_PUBLIC"] = {}
                    except Exception as err:
                        del o_meta["datePublished"]
                        converter_errors["DATE_PUBLIC"] = {}
                        converter_errors["DATE_PUBLIC"]["error"] = "v4 DATE_PUBLIC is invalid"
                        converter_errors["DATE_PUBLIC"]["message"] = str(err)
                        pass
                elif o_meta["state"] == "PUBLISHED":
                    o_meta["datePublished"] = str(getDatePublished(o_meta["cveId"], recordHistory))
                        
                if "datePublished" in o_meta and o_meta["datePublished"] == "":
                    del o_meta["datePublished"]
                elif "datePublished" in o_meta:
                    try:
                        dt = dateParse(o_meta["datePublished"])
                    except:
                        del o_meta["datePublished"]

                if "DATE_REQUESTED" in i_meta and i_meta["DATE_REQUESTED"] != "":
                    try:
                        o_meta["dateReserved"] = i_meta["DATE_REQUESTED"]
                        if not isinstance(o_meta["dateReserved"], datetime.datetime):
                            o_meta["dateReserved"] = str(datetime.datetime.combine(dateParse(o_meta["dateReserved"]).date(), datetime.datetime.min.time()).isoformat())
                        keys_used["PUBLIC"]["DATE_REQUESTED"] = {}
                    except Exception as err:
                        converter_errors["DATE_REQUESTED"] = {}
                        converter_errors["DATE_REQUESTED"]["error"] = "v4 DATE_REQUESTED is invalid"
                        converter_errors["DATE_REQUESTED"]["message"] = str(err)
                else:
                    o_meta["dateReserved"] = str(getReservedDate(o_meta["cveId"], recordHistory))
                    if not isinstance(o_meta["dateReserved"], datetime.datetime):
                        o_meta["dateReserved"] = str(datetime.datetime.combine(dateParse(o_meta["dateReserved"]).date(), datetime.datetime.min.time()).isoformat())
                    
            else:
                raise MissingRequiredPropertyValue(inputfile, "CVE_data_meta no STATE")
        except Exception as e:
            print( inputfile + " :: " + str(e) )
            print( traceback.format_exc() )
            if type(e) is not MissingRequiredPropertyValue:
                raise MissingRequiredPropertyValue(inputfile, "CVE_data_meta structure error")
            else:
                raise e

        ludate = getLastUpdated(o_meta["cveId"], recordHistory)
        if ludate:
            o_meta["dateUpdated"] = str(ludate)
        else:
            o_meta["dateUpdated"] = str(datetime.datetime.combine(datetime.date.today(), datetime.datetime.min.time()).isoformat())

        jout["cveMetadata"] = o_meta

        # public up convert
        if o_meta["state"].upper() == "PUBLISHED":
            o_cna = {}
            if "TITLE" in i_meta and i_meta["TITLE"] != "": 
                o_cna["title"] = i_meta["TITLE"]
                maxTitle = max(maxTitle, len(o_cna["title"]))
                if len(o_cna["title"]) > v5MaxTitleLength:
                    o_cna["title"] = (o_cna["title"][:(v5MaxTitleLength - 5)] + " ...")
                    converter_errors["TITLE"] = {"error": "TITLE too long. Truncating in v5 record.", "message": "Truncated!"}

                keys_used["PUBLIC"]["TITLE"] = {}
                
            if "DATE_PUBLIC" in i_meta:
                o_cna["datePublic"] = i_meta["DATE_PUBLIC"]
                try:
                    if not isinstance(o_cna["datePublic"], datetime.datetime):
                        o_cna["datePublic"] = str(datetime.datetime.combine(dateParse(o_cna["datePublic"]).date(), datetime.datetime.min.time()).isoformat())
                    keys_used["PUBLIC"]["DATE_PUBLIC"] = {}
                except Exception as err:
                    del o_cna["datePublic"]
                    pass
                
                if "datePublic" in o_cna and o_cna["datePublic"] == "":
                    del o_cna["datePublic"]
                elif "datePublic" in o_cna:
                    try:
                        dt = dateParse(o_cna["datePublic"])
                    except:
                        print("removing datePublic")
                        del o_cna["datePublic"]

                
            if "DATE_ASSIGNED" in i_meta:
                try:
                    o_cna["dateAssigned"] = i_meta["DATE_ASSIGNED"]
                    if not isinstance(o_cna["dateAssigned"], datetime.datetime):
                        o_cna["dateAssigned"] = str(datetime.datetime.combine(dateParse(o_cna["dateAssigned"]).date(), datetime.datetime.min.time()).isoformat())

                    keys_used["PUBLIC"]["DATE_ASSIGNED"] = {}
                except Exception as err:
                    converter_errors["DATE_ASSIGNED"] = {}
                    converter_errors["DATE_ASSIGNED"]["error"] = "v4 DATE_ASSIGNED is invalid"
                    converter_errors["DATE_ASSIGNED"]["message"] = str(err)

            # get org info
            o_cna["providerMetadata"] = {}
            o_cna["providerMetadata"]["orgId"] = o_meta["assignerOrgId"]
            o_cna["providerMetadata"]["shortName"] = o_meta["assignerShortName"]
            try:
                o_cna["providerMetadata"]["dateUpdated"] = o_meta["dateUpdated"]
                if not isinstance(o_cna["providerMetadata"]["dateUpdated"], datetime.datetime):
                    o_cna["providerMetadata"]["dateUpdated"] = str(datetime.datetime.combine(dateParse(o_cna["providerMetadata"]["dateUpdated"]).date(), datetime.datetime.min.time()).isoformat())
            except:
                o_cna["providerMetadata"]["dateUpdated"] = str(datetime.datetime.combine(dateParse(datetime.now(), datetime.datetime.min.time()).isoformat()))
            

            if "description" in data and "description_data" in data["description"]:
                keys_used["PUBLIC"]["description"] = ""
                o_cna["descriptions"] = []
                for i_desc in data["description"]["description_data"]:
                    o_desc = {}
                    if "lang" in i_desc: 
                        o_desc["lang"] = lang_code_2_from_3(i_desc["lang"])
                    
                    newDesc = i_desc["value"]
                                            
                    # find and convert description tags - DISPUTED, UNSUPPORTED WHEN ASSIGNED
                    if i_desc["value"].casefold().startswith("** disputed"):
                        if "tags" not in o_cna:
                            o_cna["tags"] = []
                        if "disputed" not in o_cna["tags"]:
                            o_cna["tags"].append("disputed")
                        newDesc = newDesc[14:-1].strip()
                    
                        
                    if i_desc["value"].casefold().startswith("** unsupported when assigned"):
                        tagval = "unsupported-when-assigned"
                        if "tags" not in o_cna:
                            o_cna["tags"] = []
                        if tagval not in o_cna["tags"]:
                            o_cna["tags"].append(tagval)
                        newDesc = newDesc[31:-1].strip()

                    if "value" in i_desc: o_desc["value"] = newDesc
                    o_cna["descriptions"].append(o_desc)
                    
                    

            if "affects" in data:
                keys_used["PUBLIC"]["affects"] = ""
                o_cna["affected"] = {}
                i_affects = data["affects"]
                o_affected = []
                #vendors
                if "vendor" in i_affects:
                    for i_vd in i_affects["vendor"]["vendor_data"]:
                        if "product" in i_vd and "product_data" in i_vd["product"]:
                            for vd_pd in i_vd["product"]["product_data"]:
                                if "version" in vd_pd and "version_data" in vd_pd["version"]:
                                    v_agg_hash = {}
                                    v_agg_list = {}
                                    product_name = vd_pd["product_name"]
                                    for pd_vd in vd_pd["version"]["version_data"]:
                                        if not "version_value" in pd_vd: 
                                            # throw invalid version_data, must have version_value value
                                            raise MissingRequiredPropertyValue(o_meta["cveId"], "AFFECT.vendor.product  missing a version_value for ("+i_vd["vendor_name"]+" - "+vd_pd["product_name"]+")")
                                        platform = ""
                                        if "platform" in pd_vd:
                                            platform = pd_vd["platform"]
                                        if not platform in v_agg_hash:
                                            v_agg_hash[platform] = {}
                                            v_agg_list[platform] = []
                                        vn_hash = v_agg_hash[platform]
                                        v_list = v_agg_list[platform]
                                        if "version_name" in pd_vd:  # vulnogram generated                                           
                                            vn = pd_vd["version_name"]
                                            if product_name.casefold() is not vn.casefold():
                                                [vstatus, va] = convert_VA(pd_vd)
                                                if va == '=':
                                                    v_list.append(nonEmpty(eq_version(pd_vd, vstatus)))
                                                elif vn in vn_hash:
                                                    if va == '<':
                                                        vstatus = negate(vstatus)
                                                    if va == '<=':
                                                        vstatus = negate(vstatus)
                                                        pd_vd["version_value"] = pd_vd["version_value"] + ' +1'
                                                    else:
                                                        if not "changes" in vn_hash[vn]:
                                                            vn_hash[vn]["changes"] = []
                                                        chg = {
                                                            "at": pd_vd["version_value"],
                                                            "status": vstatus
                                                        }
                                                        if chg not in vn_hash[vn]["changes"]:
                                                            vn_hash[vn]["changes"].append(chg)
                                                elif va == '<':
                                                    vn_hash[vn] = nonEmpty(l_version(pd_vd, vstatus))
                                                elif va == '<=':
                                                    vn_hash[vn] = nonEmpty(le_version(pd_vd, vstatus))
                                                else:
                                                    vn_hash[vn] = {
                                                        "version": pd_vd["version_value"],
                                                        "status": vstatus,
                                                        "lessThan": pd_vd["version_name"] + '*',
                                                        "versionType": "custom"
                                                    }
                                            # end if product_name is not version_name
                                        else:
                                            [vstatus, va] = convert_VA(pd_vd)
                                            version_value = pd_vd["version_value"]
                                            if version_value:
                                                version_value = version_value.strip()
                                            
                                            if not version_value or len(version_value) < 1:
                                                version_value = "undefined"
                                            
                                            if va == '=':
                                                v_list.append(nonEmpty({
                                                    "version": pd_vd["version_value"],
                                                    "status": vstatus
                                                }))
                                            elif va == '<':
                                                v_list.append(nonEmpty({
                                                    "version": 'unspecified',
                                                    "lessThan": version_value,
                                                    "status": vstatus,
                                                    "versionType": "custom"
                                                }))
                                            elif va == '<=':
                                                v_list.append(nonEmpty({
                                                    "version": 'unspecified',
                                                    "lessThanOrEqual": version_value,
                                                    "status": vstatus,
                                                    "versionType": "custom"
                                                }))
                                            elif va == '>':
                                                v_list.append(nonEmpty({
                                                    "version": "next of " + pd_vd["version_value"],
                                                    "status": vstatus,
                                                    "lessThan": "unspecified",
                                                    "versionType": "custom"
                                                }))
                                            elif va == '>=':
                                                v_list.append(nonEmpty({
                                                    "version": pd_vd["version_value"],
                                                    "status": vstatus,
                                                    "lessThan": "unspecified",
                                                    "versionType": "custom"
                                                }))
                                            else:
                                                v_list.append(nonEmpty({
                                                    "version": pd_vd["version_value"],
                                                    "status": "affected",
                                                }))                                                
                                        
                                            # check for blank version and defailt to "unspecified"
                                            #if len(version_item["version"]) < 1:
                                            #    version_item["version"] = "unspecified"
                                        # end if version_name in pd_vd

                                    for platform in v_agg_hash:
                                        # build affected item here:
                                        affected_item = {}
                                        affected_item["vendor"] = i_vd["vendor_name"]
                                        affected_item["product"] = vd_pd["product_name"]
                                        affected_item["versions"] = []
                                        if platform != "":
                                            affected_item["platforms"] = [platform]
                                        if len(v_agg_list[platform]) > 0:
                                            affected_item["versions"].extend(v_agg_list[platform])
                                        if v_agg_hash[platform]:
                                            affected_item["versions"].extend(v_agg_hash[platform].values())

                                        #remove duplicates
                                        y = []
                                        for x in affected_item["versions"]:
                                            if not x in y:
                                                y.append(x)
                                        if len(y) > 0:
                                            affected_item["versions"] = y
                                        else:
                                            del affected_item["versions"]

                                        # defaultStatus is new, default to 'unknown' if versions is empty
                                        if not "versions" in affected_item:
                                            affected_item['defaultStatus'] = "unknown"
                                
                                        # end for loop of version_data
                                        o_affected.append(affected_item)

                # clean affect before adding
                # - truncate long fields                
                # - populate missing required fields
                for o in o_affected:
                    if "vendor" not in o or not o["vendor"]:
                        o["vendor"] = "unspecified"

                    if "product" not in o or not o["product"]:
                        o["product"] = "unspecified"

                    for vo in o["versions"]:
                        if len(vo["version"]) > maxV5VersionLength:
                            vo["version"] = (vo["version"][:(maxV5VersionLength-16)] + " ...[truncated*]")
                            converter_errors["version_name"] = {"error": "version_name too long. Use array of versions to record more than one version.", "message": "Truncated!"}

                    if len(o["product"]) > maxV5ProductLength:
                        o["product"] = (o["product"][:(maxV5ProductLength-16)] + " ...[truncated*]")
                        converter_errors["product_name"] = {"error": "product_name too long. Use array of products to recond more than one product.", "message": "Truncated!"}

                o_cna["affected"] = o_affected
            # done with affected up convert
            
            if "references" in data and "reference_data" in data["references"]:
                keys_used["PUBLIC"]["references"] = ""
                o_cna["references"] = []
                for i_ref in data["references"]["reference_data"]:
                    if "refsource" in i_ref and i_ref["refsource"] == "url":
                        # drop references of resource type == 'url'
                        pass
                    else:
                        o_ref = {}
                        #ignore name if empty or if same as URL
                        if "name" in i_ref and i_ref["name"] != "" and i_ref["name"] != i_ref["url"] : o_ref["name"] = i_ref["name"]
                        if "refsource" in i_ref:
                            if "tags" not in o_ref:
                                o_ref["tags"] = []

                            # convert to new reference tags
                            v5Tag_values = getV5ReferenceTagValue(i_ref["refsource"])
                            if v5Tag_values:
                                for v5Tag in v5Tag_values:
                                    if v5Tag not in o_ref["tags"]:
                                        o_ref["tags"].append(v5Tag)
                                
                            # preserve legacy tag value
                            refSourceTag = "x_refsource_"+i_ref["refsource"]
                            if refSourceTag not in o_ref["tags"]:
                                o_ref["tags"].append(refSourceTag)

                        if "url" in i_ref: o_ref["url"] = i_ref["url"]

                        # decode then encode URL, to clear issue with AJV URL validations
                        o_ref["url"] = reEncodeUrl(o_ref["url"])

                        # check to ensure unique reference before adding
                        if (o_ref not in o_cna["references"]
                                and o_ref["url"]):
                            o_cna["references"].append(o_ref)
                    # end if resource != 'url'
            # end of reference up convert    

            if "credit" in data: # may be a list, or a string
                keys_used["PUBLIC"]["credit"] = ""
                if isinstance(data["credit"], list):
                    for i_credit in data["credit"]:
                        if isinstance(i_credit, dict):
                            o_credit = {}
                            if "lang" in i_credit and "value" in i_credit:
                                o_credit["lang"] = lang_code_2_from_3(i_credit["lang"])
                            else:
                                o_credit["lang"] = "en"
                            
                            if "value" in i_credit:
                                if "credits" not in o_cna:
                                    o_cna["credits"] = []
                                o_credit["value"] = i_credit["value"]                        
                                o_cna["credits"].append(o_credit)
                        elif isinstance(i_credit, list):
                            for citem in i_credit:
                                o_credit = {}
                                o_credit["lang"] = "en"
                                if "credits" not in o_cna:
                                    o_cna["credits"] = []
                                o_credit["value"] = citem                        
                                o_cna["credits"].append(o_credit)
                        else:
                            o_credit = {}
                            o_credit["lang"] = "en"
                            o_credit["value"] = i_credit                        
                            if "credits" not in o_cna:
                                o_cna["credits"] = []
                            o_cna["credits"].append(o_credit)
                        
                else:
                    # convert value content to string
                    o_cna["credits"] = []
                    o_credit = {}
                    o_credit["lang"] = "en"
                    o_credit["value"] = str(data["credit"])
                    o_cna["credits"].append(o_credit)
            # end of credit up convert    
                        
            if "impact" in data and data["impact"] and not(data["impact"] is None): # impact is an unofficial community added property under CVE 4.0 that maps to metrics array in CVE 5
                keys_used["PUBLIC"]["impact"] = ""
                try:
                    o_cna["metrics"] = []
                    for i_impact in data["impact"]:
                        o_impact = {}
                        
                        iver = "other"
                        iobj = {}
                        if isinstance(data["impact"], collections.abc.Mapping):  # if impact is a JSON object
                            # check key value, try to match on recognized versions
                            if i_impact == "cvss" and "version" in data["impact"][i_impact]:                            
                                if data["impact"][i_impact]["version"] == "3.1":
                                    iver = "cvssV3_1"
                                elif data["impact"][i_impact]["version"] == "3.0":
                                    iver = "cvssV3_0"
                                elif data["impact"][i_impact]["version"] == "2.0":
                                    iver = "cvssV2_0"                            
                                else:
                                    pass
                                iobj = data["impact"][i_impact]
                            elif i_impact == "cvssv3":
                                iver = "cvssV3_0"
                                iobj = data["impact"][i_impact]
                            elif i_impact == "cvss" and isinstance(data["impact"][i_impact], list):
                                for tc in data["impact"][i_impact]: # array of arrays
                                    if ( isinstance(tc, list) ): # list in list
                                        for ic in tc: # inner array
                                            lver = "other"
                                            iver = "skip" #skip the external o_impact because we found an array instead of a object                                            
                                            if "version" in ic:
                                                if ic["version"] == "3.1":
                                                    lver = "cvssV3_1"
                                                elif ic["version"] == "3.0":
                                                    lver = "cvssV3_0"
                                                elif ic["version"] == "2.0":
                                                    lver = "cvssV2_0"
                                                else:
                                                    bv = i_impact + "-" +ic[version]
                                                    if bv not in invalid_impact_versions:
                                                        invalid_impact_versions[bv] = {}
                                                        invalid_impact_versions[bv]["count"] = 0
                                                    invalid_impact_versions[bv]["count"] += 1
                                                    pass
                                            else:
                                                # print("didn't find version")
                                                # print(ic)
                                                raise MissingRequiredPropertyValue(i_meta["ID"], "IMPACT.version from cvss[[{}]]" )
                                            
                                            if lver == "other":
                                                o_impact[lver] = {}
                                                o_impact[lver]["type"] = "unknown"
                                                o_impact[lver]["content"] = ic
                                            else:
                                                o_impact[lver] = ic.copy() 
                                    elif (isinstance(tc, collections.abc.Mapping)): # array of objects
                                            lver = "other"
                                            iver = "skip" #skip the external o_impact because we found an array instead of a object                                            
                                            if "version" in tc:
                                                if tc["version"] == "3.1":
                                                    lver = "cvssV3_1"
                                                elif tc["version"] == "3.0":
                                                    lver = "cvssV3_0"
                                                elif tc["version"] == "2.0":
                                                    lver = "cvssV2_0"
                                                else:
                                                    bv = i_impact + "-" +tc[version]
                                                    if bv not in invalid_impact_versions:
                                                        invalid_impact_versions[bv] = {}
                                                        invalid_impact_versions[bv]["count"] = 0
                                                    invalid_impact_versions[bv]["count"] += 1
                                                    pass
                                            else:
                                                # print("didn't find version")
                                                # print(tc)
                                                raise MissingRequiredPropertyValue(i_meta["ID"], "IMPACT.version from cvss[{}]" )
                                            
                                            if lver == "other":
                                                o_impact[lver] = {}
                                                o_impact[lver]["type"] = "unknown"
                                                o_impact[lver]["content"] = tc
                                            else:
                                                o_impact[lver] = tc.copy() 
                                    else:
                                        raise UnexpectedPropertyValue( i_meta["ID"], "Impact - cvss structure not recognized")

                            else: # impact not an object, or property name not recognized
                                pass
                                    
                            if iver == "other":
                                # ensure content is an object
                                o_impact[iver] = buildImpactOther(i_impact, data["impact"][i_impact])
                                    
                            elif iver == "skip":
                                pass
                            else:
                                o_impact[iver] = data["impact"][i_impact].copy()
                        else:  # impact was not a JSON object, just copy the content and mark type as unknown
                            c_i_impact = clean_empty(i_impact)
                            if c_i_impact:
                                o_impact[iver] = buildImpactOther(i_impact, c_i_impact)
                            

                        # record if a scoring element landed in "other"
                        # just upconversion tracking log
                        if o_impact and i_impact != "other":
                            # print("have impact")
                            if "other" in o_impact:
                                # print("have converted other impact:" + str(i_impact))
                                # print(json.dumps(o_impact, indent=2))
                                if "content" in o_impact["other"]:
                                    if i_meta["ID"] not in scoring_other:
                                        scoring_other[i_meta["ID"]] = []
                                    scoring_other[i_meta["ID"]].append(o_impact["other"]["content"])

                        # repair cvss data conversion
                        # if any property is missing replace with generated object
                        try:
                            if "cvssV3_1" in o_impact and "vectorString" in o_impact["cvssV3_1"]:
                                vStrMatch = re.search('(([A-Z]+:[A-Z310.]+/?)+)', o_impact["cvssV3_1"]["vectorString"], re.IGNORECASE)                               
                                if vStrMatch:
                                    try:
                                        vStr = vStrMatch.group(1)
                                        if not vStr.startswith("CVSS:3."):
                                                vStr = "CVSS:3.1/"+vStr
                                        c = CVSS3(vStr)
                                        o_impact["cvssV3_1"] = redux_CVSS(c.as_json(), vStr)
                                        # fix mismatched CVSS versions
                                        if o_impact["cvssV3_1"]["version"] == "3.0":
                                            o_impact["cvssV3_0"] = o_impact["cvssV3_1"]
                                            del o_impact["cvssV3_1"]
                                    except Exception as err:
                                        del o_impact["cvssV3_1"]
                                        converter_errors["cvssV3_1"] = {}
                                        converter_errors["cvssV3_1"]["error"] = "CVSSV3_1 data from v4 record is invalid"
                                        converter_errors["cvssV3_1"]["message"] = str(err)


                            if "cvssV3_0" in o_impact:
                                if "BM" in o_impact["cvssV3_0"]:
                                    o_impact["cvssV3_0"]["vectorString"] = IBM_score(o_impact["cvssV3_0"])
                                if "vectorString" in o_impact["cvssV3_0"]:
                                    vStrMatch = re.search('(([A-Z]+:[A-Z310.]+/?)+)', o_impact["cvssV3_0"]["vectorString"], re.IGNORECASE)                               
                                    if vStrMatch:
                                        try:
                                            vStr = vStrMatch.group(1)
                                            if not vStr.startswith("CVSS:3."):
                                                    vStr = "CVSS:3.0/"+vStr                                        
                                            c = CVSS3(vStr)
                                            o_impact["cvssV3_0"] = redux_CVSS(c.as_json(), vStr)
                                            #fix mismatched CVSS versions
                                            if o_impact["cvssV3_0"]["version"] == "3.1":
                                                o_impact["cvssV3_1"] = o_impact["cvssV3_0"]
                                                del o_impact["cvssV3_0"]
                                        except Exception as err:
                                            del o_impact["cvssV3_0"]
                                            # print("error cvssV3_0")
                                            # print(err) 
                                            converter_errors["cvssV3_0"] = {}
                                            converter_errors["cvssV3_0"]["error"] = "CVSSV3_0 data from v4 record is invalid"
                                            converter_errors["cvssV3_0"]["message"] = str(err)
                                            pass

                            if "cvssV2_0" in o_impact and "vectorString" in o_impact["cvssV2_0"]:
                                vStr = re.search('(([A-Z]+:[A-Z0123.]+/?)+)', o_impact["cvssV2_0"]["vectorString"], re.IGNORECASE)
                                if vStr:
                                    try:
                                        c = CVSS2(vStr.group(1))
                                        o_impact["cvssV2_0"] = redux_CVSS(c.as_json(), vStr.group(1))
                                    except Exception as err:
                                        del o_impact["cvssV2_0"]
                                        converter_errors["cvssV2_0"] = {}
                                        converter_errors["cvssV2_0"]["error"] = "CVSSV2_0 data from v4 record is invalid"
                                        converter_errors["cvssV2_0"]["message"] = str(err)

                            # delete garbage cvss entries from impact,
                            # check and purge once after all source formats are converted
                            vers = ["cvssV3_1", "cvssV3_0", "cvssV2_0"]
                            for cVer in vers:
                                deleteMe = False
                                if cVer in o_impact:
                                    # delete garbage cvss scores        
                                    if ("vectorString" not in o_impact[cVer]
                                            or not o_impact[cVer]["vectorString"] 
                                            or not re.findall('[0-9]+', o_impact[cVer]['vectorString']) ):
                                        deleteMe = True

                                    if ("baseScore" not in o_impact[cVer]
                                            or not o_impact[cVer]["baseScore"]):
                                        deleteMe = True
                
                                    if deleteMe:
                                        del o_impact[cVer]
                                    else:
                                        if ( o_impact[cVer] 
                                                and "baseScore" in o_impact[cVer]
                                                and not isinstance(o_impact[cVer]["baseScore"], Number) ):
                                            o_impact[cVer]["baseScore"] = float(o_impact[cVer]["baseScore"])

                        except Exception as err:
                            print("error")
                            print(err) 
                            traceback.print_exc()
                            converter_errors["impact_cvss"] = {}
                            converter_errors["impact_cvss"]["error"] = "CVSS data from v4 record is invalid"
                            converter_errors["impact_cvss"]["message"] = str(err)
                            pass

                        # only add if not empty
                        if o_impact:
                            o_cna["metrics"].append(o_impact)
                    # end for impact
                except Exception as e:
                    raise UnexpectedPropertyValue(i_meta["ID"], "IMPACT", str(e))
                
                # if metrics is empty, remove it now, to avoid later cleanup    
                if not o_cna["metrics"]:
                    del o_cna["metrics"]
            # end of impact up convert    

            if "problemtype" in data and "problemtype_data" in data["problemtype"]:
                keys_used["PUBLIC"]["problemtype"] = ""
                o_cna["problemTypes"] = []
                i_pds = data["problemtype"]["problemtype_data"]
                for i_pd in i_pds:
                    o_pt_desc = []
                    if "description" in i_pd:
                        for i_desc in i_pd["description"]:                        
                            o_pd = {}
                            o_pd["type"] = "text"
                            for dk in i_desc:
                                if dk == "lang":
                                    o_pd["lang"] = lang_code_2_from_3(i_desc[dk])
                                elif dk == "value":
                                    o_pd["description"] = i_desc[dk]
                                    # If description mentions CWEs pick the first as the CWE ID
                                    cwes = re.findall(r'\bCWE-[1-9]\d*\b', i_desc[dk], flags=re.IGNORECASE)
                                    if len(cwes) > 0:
                                        o_pd["type"] = "CWE"
                                        o_pd["cweId"] = cwes[0].upper()
                                else:
                                    o_pd[dk] = i_desc[dk]
                            if "lang" not in o_pd or not o_pd["lang"]:
                                o_pd["lang"] = "en"
                            if ("description" in o_pd 
                                    and o_pd["description"] != ""):
                                o_pt_desc.append(o_pd)                
                    if "CWE-ID" in i_pd:
                        # extract all id by regex pattern, copy 
                        ids = re.findall(r'^CWE-[1-9][0-9]+$', i_pd["CWE-ID"])
                        for c in ids:
                            o_pd = {}
                            o_pd["description"] = i_pd["CWE-ID"]
                            o_pd["lang"] = "eng"
                            o_pd["type"] = "CWE"
                            o_pd["cweId"]  = c
                            o_pt_desc.append(o_pd)                
                                                
                    o_pt_descs = {}
                    if len(o_pt_desc)>0 and hasVal(o_pt_desc):
                        o_pt_descs["descriptions"] = o_pt_desc
                        o_cna["problemTypes"].append( o_pt_descs)
            # end of problem_type up convert    

            if "generator" in data: #community field
                keys_used["PUBLIC"]["generator"] = ""
                try:
                    o_cna["x_generator"] = data["generator"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "generator", "JSON not convertable")
            # end of generator up convert    

            if "source" in data: #community field
                keys_used["PUBLIC"]["source"] = ""
                try:
                    o_cna["source"] = data["source"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "source", "JSON not convertable")
            # end of source up convert    

            if "configuration" in data:
                keys_used["PUBLIC"]["configuration"] = ""
                try:
                    if isinstance(data["configuration"], list):                
                        o_cna["configurations"] = data["configuration"]
                    else:
                        o_cna["configurations"] = []
                        o_cna["configurations"].append(data["configuration"])
                    o_cna["configurations"] = convertLangInArray(o_cna["configurations"])  # language code conversion
                    if len(o_cna["configurations"]) < 1:
                        del o_cna["configurations"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "configuration", "JSON not convertable")
            # end of configuration up convert    

            if "work_around" in data:
                keys_used["PUBLIC"]["work_around"] = ""
                try:
                    if isinstance(data["work_around"], list):                
                        o_cna["workarounds"] = data["work_around"]
                    else:
                        o_cna["workarounds"] = []
                        o_cna["workarounds"].append(data["work_around"])
                        
                    o_cna["workarounds"] = convertLangInArray(o_cna["workarounds"])  # language code conversion
                    if len(o_cna["workarounds"]) < 1:
                        del o_cna["workarounds"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "work_around", "JSON not convertable")
            # end of work_around up convert    

            if "workaround" in data:
                keys_used["PUBLIC"]["workaround"] = ""
                try:
                    if isinstance(data["workaround"], list):                
                        o_cna["workarounds"] = data["workaround"]
                    else:
                        o_cna["workarounds"] = []
                        o_cna["workarounds"].append(data["workaround"])
                        
                    o_cna["workarounds"] = convertLangInArray(o_cna["workarounds"])  # language code conversion
                    if len(o_cna["workarounds"]) < 1:
                        del o_cna["workarounds"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "work_around", "JSON not convertable")
            # end of work_around up convert    

            if "exploit" in data:
                keys_used["PUBLIC"]["exploit"] = ""
                try:
                    if isinstance(data["exploit"], list):                
                        o_cna["exploits"] = data["exploit"]
                    else:
                        o_cna["exploits"] = []
                        o_cna["exploits"].append(data["exploit"])
                    o_cna["exploits"] = convertLangInArray(o_cna["exploits"])  # language code conversion
                    if len(o_cna["exploits"]) < 1:
                        del o_cna["exploits"]                   
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "exploit", "JSON not convertable")
            # end of exploit up convert    

            if "timeline" in data:
                # v4 time is supposed to be an array of object with time, lang, value properties
                keys_used["PUBLIC"]["timeline"] = ""
                try:
                    if isinstance(data["timeline"], list):                
                        o_cna["timeline"] = data["timeline"]
                    else:
                        o_cna["timeline"] = []
                        o_cna["timeline"].append(data["timeline"])
                    o_cna["timeline"] = convertLangInArray(o_cna["timeline"])  # language code conversion
                    if len(o_cna["timeline"]) < 1:
                        del o_cna["timeline"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "timeline", "JSON not convertable")
                    
                # clean up, remove missing value, convert to datetime for time
                if "timeline" in o_cna:
                    for t in o_cna["timeline"]:
                        if ("value" not in t or not t["value"]
                                or "time" not in t or not t["time"]):
                            o_cna["timeline"].remove(t)
                        else:
                            # ensure a lang is present default to en
                            if "lang" not in t:
                                t["lang"] = "en"
                            # ensure time is in datetime format
                            if not isinstance(t["time"], datetime.datetime):
                                t["time"] = str(datetime.datetime.combine(dateParse(t["time"]).date(), datetime.datetime.min.time()).isoformat())
            # end of timeline up convert    

            if "solution" in data:
                keys_used["PUBLIC"]["solution"] = ""
                try:
                    if isinstance(data["solution"], list):                
                        o_cna["solutions"] = data["solution"]
                    else:
                        o_cna["solutions"] = []
                        o_cna["solutions"].append(data["solution"])
                    o_cna["solutions"] = convertLangInArray(o_cna["solutions"])  # language code conversion
                    if len(o_cna["solutions"]) < 1:
                        del o_cna["solutions"]
                except:
                    raise UnexpectedPropertyValue(o_meta["cveId"], "source", "JSON not convertable")
                    
                # purge incomplete entries from solutions, and set lang if missing
                if "solutions" in o_cna:
                    for s in o_cna["solutions"]:
                        if ("value" not in s
                                or not s["value"]):
                            o_cna["solutions"].remove(s)
                        else:
                            if "lang" not in s:
                                s["lang"] = "en"
            # end of solution up convert    

            # add extra / non-standard content to CNA container to avoid data loss.
            for i_key in data:
                if o_meta["state"] in keys_used and i_key not in keys_used[o_meta["state"]]:
                    # skip old root fields not converted
                    if i_key not in ["data_format", "data_type", "data_version"]:
                        o_key = i_key
                        if not o_key.startswith("x_"):
                            o_key = "x_" + o_key
                        o_cna[o_key] = data[i_key]

            # drop empty propteries
            if not "affected" in o_cna:
                o_cna["affected"] = [{"vendor": "unspecified", "product": "unspecified", "defaultStatus": "unknown"}]
                converter_errors["affects"] = {"error": "Missing affected product. Using unspecified instead.", "message": "Marking it unspecified!"}
            o_cna = clean_empty(o_cna)

            # insert source record                                                 
            o_cna["x_legacyV4Record"] = data

            jout["containers"] = {}
            jout["containers"]["cna"] = o_cna
            writeout = True
            
        elif o_meta["state"].upper() == "RESERVED":
            writeout = False            
            
        elif o_meta["state"].upper() == "REJECTED":
            o_cna = {}
            o_cna["providerMetadata"] = {}
            o_cna["providerMetadata"]["orgId"] = o_meta["assignerOrgId"]
            o_cna["providerMetadata"]["shortName"] = o_meta["assignerShortName"]
            try:
                o_cna["providerMetadata"]["dateUpdated"] = o_meta["dateUpdated"]
                if not isinstance(o_cna["providerMetadata"]["dateUpdated"], datetime.datetime):
                    o_cna["providerMetadata"]["dateUpdated"] = str(datetime.datetime.combine(dateParse(o_cna["providerMetadata"]["dateUpdated"]).date(), datetime.datetime.min.time()).isoformat())
            except:
                o_cna["providerMetadata"]["dateUpdated"] = str(datetime.datetime.combine(dateParse(datetime.now(), datetime.datetime.min.time()).isoformat()))
        
            # o_meta['dateRejected'] = o_meta["dateUpdated"]
            o_meta['dateRejected'] = str(getRejectedDate(o_meta["cveId"], recordHistory))

            if not isinstance(o_meta["dateRejected"], datetime.datetime):
                o_meta["dateRejected"] = str(datetime.datetime.combine(dateParse(o_meta["dateRejected"]).date(), datetime.datetime.min.time()).isoformat())

            if "description" in data and "description_data" in data["description"]:
                keys_used["REJECT"]["description"] = ""
                o_cna["rejectedReasons"] = []
                for i_desc in data["description"]["description_data"]:
                    o_desc = {}
                    if "lang" in i_desc:
                        o_desc["lang"] = lang_code_2_from_3(i_desc["lang"])
                    if "value" in i_desc: 
                        o_desc["value"] = i_desc["value"]
            
                    # find and convert description tags - DISPUTED, UNSUPPORTED WHEN ASSIGNED
                    if o_desc["value"].casefold().startswith("** disputed"):
                        if "tags" not in o_cna:
                            o_cna["tags"] = []
                        if "disputed" not in o_cna["tags"]:
                            o_cna["tags"].append("disputed")
                        o_desc["value"] = o_desc["value"][14:-1].strip()
                        
                    if o_desc["value"].casefold().startswith("** unsupported when assigned"):
                        tagval = "unsupported-when-assigned"
                        if "tags" not in o_cna:
                            o_cna["tags"] = []
                        if tagval not in o_cna["tags"]:
                            o_cna["tags"].append(tagval)
                        o_desc["value"] = o_desc["value"][31:-1].strip()

                    if o_desc["value"].casefold().startswith("** reject"):
                        o_desc["value"] = o_desc["value"][13:-1].strip()

                    o_cna["rejectedReasons"].append(o_desc)
            
                    
            # if replaced by present
            if "REPLACED_BY" in i_meta:
                rep_ids = i_meta["REPLACED_BY"].split(',')
                for ri in rep_ids:
                    if not "replacedBy" in o_meta: o_meta["replacedBy"] = []
                    o_meta["resplacedBy"].append(ri)

            # drop empty propteries
            o_cna = clean_empty(o_cna)

            jout["containers"] = {}
            jout["containers"]["cna"] = o_cna
            writeout = True
            pass
        else:
            writeout = False
            raise UnexpectedPropertyValue("STATE", o_meta["state"])

        # if there were converter errors, add them to the result now
        # this will force a validation error
        if len(converter_errors) > 0:
            jout["containers"]["cna"]["x_ConverterErrors"] = converter_errors      
            if "impact_cvss" in converter_errors:
                global cvssErrorList
                cvssErrorList.append({o_meta["cveId"]:converter_errors["impact_cvss"]})


        if writeout:
            #attempt JSON validation
            global JSONValidator
            global JSONValidatorPublished
            global ValidationFailures
            
            if not JSONValidator:
                # print("v5schemaPath = " + v5SchemaPath)            
                global v5SchemaPath
                JSONValidator = jsonschema.Draft7Validator(json.load(open(v5SchemaPath)))
            if not JSONValidatorPublished:
                global v5SchemaPath_published
                JSONValidatorPublished = jsonschema.Draft7Validator(json.load(open(v5SchemaPath_published)))

            valErrors = None
            if jout["cveMetadata"]["state"] == "PUBLISHED":
                valErrors = JSONValidatorPublished.iter_errors(jout)
            elif jout["cveMetadata"]["state"] == "REJECTED":
                valErrors = JSONValidator.iter_errors(jout)
            elif jout["cveMetadata"]["state"] == "RESERVED":
                # print(jout["cveMetadata"]["cveId"] + " state = " + jout["cveMetadata"]["state"]) 
                # valErrors = JSONValidator.iter_errors(jout)
                pass
            else:
                print(jout["cveMetadata"]["cveId"] + " state = " + jout["cveMetadata"]["state"]) 
                valErrors = JSONValidator.iter_errors(jout)
            
            if valErrors:
                errors = []
                for error in valErrors:
                    errors.append( str(error.json_path) + " -- validator = "+ str(error.validator)) 
                                   
                if len(errors) > 0:
                    jout["containers"]["cna"]["x_ValidationErrors"] = errors
                    # ValidationFailures.append( jout["cveMetadata"]["cveId"] )
                    ValidationFailures[jout["cveMetadata"]["cveId"]] = jout["containers"]["cna"]["x_ValidationErrors"]
            
            # write result to file of CVE ID
            fname = os.path.join( outputpath, jout["cveMetadata"]["cveId"] + ".json")
            os.makedirs(outputpath, exist_ok=True)
            jout_file = open(fname, "w")
            jout_file.write( json.dumps(jout, sort_keys=True, indent=4) )
            jout_file.close

        for i_key in data:
            if (i_key in keys_used[i_meta["STATE"]] or
                i_key in ['data_type', 'data_version', 'data_format']
                or i_meta["STATE"] == "RESERVED"):
                #root key was converted
                pass
            else:
                #found a key that was not explicitly converted
                #these CVEs should be reviewed for validity.
                if o_meta["state"] not in extra_keys: extra_keys[o_meta["state"]] = {}
                if i_key not in extra_keys[o_meta["state"]]: extra_keys[o_meta["state"]][i_key] = []
                if o_meta["cveId"] not in extra_keys[o_meta["state"]][i_key]: extra_keys[o_meta["state"]][i_key].append(o_meta["cveId"])
    

class UnexpectedPropertyValue(Exception):
    def __init__(self, cveid, propertyname, message="unexpected value in property"):
        self.propertyname = propertyname
        self.cveid = cveid
        self.message = message
        super().__init__(self.message)
    def __str__(self):
        return self.cveid + " - " + self.propertyname + " - " + self.message 

class MissingRequiredPropertyValue(Exception):
    def __init__(self, cveid, propertyname, message="Required property missing from CVE"):
        self.propertyname = propertyname
        self.cveid = cveid
        self.message = message
        super().__init__(self.message)
    def __str__(self):
        return self.cveid + " - " + self.propertyname + " - " + self.message 


def getOrgUUID( short_name ):
    global all_orgs
    
    if not all_orgs or len(all_orgs) < 1: getOrgData()

    # try/except block to catch integrity error in case the org doesn't exist
    uuid = None
    try:
        for org in all_orgs:
            # print( json.dumps(all_orgs, indent=2))
            orgShortName = all_orgs[org]["short_name"]
            if orgShortName == short_name:
                uuid = all_orgs[org]["UUID"]
                break
    except:
        pass
    return uuid


def getOrgShortName( org_uuid ):
    global all_orgs
    
    if not all_orgs or len(all_orgs) < 1: getOrgData()

    # try/except block to catch integrity error in case the org doesn't exist
    orgsn = None
    if org_uuid in all_orgs:
        if "short_name" in all_orgs[org_uuid]:
            orgsn = all_orgs[org_uuid]["short_name"]
    return orgsn


def getAllUsers():
    global all_orgs
    global all_users
    global user_errors
    
    if not all_orgs or len(all_orgs) < 1: getOrgData()

    # try/except block to catch integrity error in case the org doesn't exist
    try:
        for org in all_orgs:
            # print( json.dumps(all_orgs, indent=2))
            orgShortName = all_orgs[org]["short_name"]
            USERS_URL = settings.AWG_IDR_SERVICE_URL + '/org/' + orgShortName + '/users'
            users_params = {}
            # Attempt to get org from RSUS
            users_result = call_idr_service('get', BASE_HEADERS, USERS_URL, users_params)
            data = json.loads(users_result)
            for u in data["users"]:
                # add org short_name to user object
                u["org_short_name"] = orgShortName
                # only keep first org match, else record as error
                if u["username"] in all_users:
                    if u["username"] not in user_errors:
                        user_errors[u["username"]] = []
                    user_errors[u["username"]].append("User in multiple orgs with: "+orgShortName)
                else:
                    all_users[u["username"]] = u

        # add default user
        d_user = {}
        d_user["username"] = settings.AWG_USER_NAME
        d_user["org_short_name"] = settings.AWG_ORG_SHORT_NAME
        d_user["org_UUID"] = settings.AWG_USER_ORG_UUID
        d_user["UUID"] = settings.AWG_USER_UUID
        all_users["DEFAULT"] = d_user                
            
    except Exception as e:
        print(str(e))
        raise e
    return True    



def getIDRInfo(cveId, delay=300, retry=0):
    global IDRCollection
    data = None
    if not IDRCollection:
        try:
            with open("cve_ids.json") as cveids:
                lines = cveids.readlines()
                lines = [line.rstrip() for line in lines]
                for line in lines:
                    jline = json.loads(line)
                    IDRCollection[ jline["cve_id"] ] = jline    
        except Exception as e:
            print("bulk IDR ERROR: "+str(e))
    
    if cveId in IDRCollection:
        # if IDR data present from bulkgrab use it
        data = IDRCollection[cveId]
    else:
        print("Services export miss on " + cveId)
        # if IDR data is not in buldgrab, get and add it.
        IDR_URL = settings.AWG_IDR_SERVICE_URL + '/cve-id/' + cveId
        idr_params = {}
        data = None
        
        # try/except block to catch integrity error in case the org doesn't exist
        try:
            # Attempt to get org from RSUS
            idr_result = call_idr_service('get', BASE_HEADERS, IDR_URL, idr_params)
            if idr_result and idr_result.startswith("{"):
                data = json.loads(idr_result)
                if not data["cve_id"] in IDRCollection:
                    IDRCollection[data["cve_id"]] = []
                IDRCollection[data["cve_id"]].append(data)
                
            else:
                if retry < 14:
                    print("delaying for: "+ str(delay) + " -- on -- " + cveId)
                    time.sleep(delay)
                    data = getIDRInfo(cveId, delay, retry+1)    
                else:
                    print("Record Timeout Issue - URL - " + IDR_URL)
                    # print(str(idr_result))
        except Exception as e:
            if retry < 14:
                # if delay > 179:
                print( str(e))
                print("Exception delay for: " + str(delay))
                print(" --- " + IDR_URL)
                time.sleep(delay)
                data = getIDRInfo(cveId, delay, retry+1)    
            else:
                # print(str(idr_result))
                print("Exception Failed -- get IDR info -- URL - " + IDR_URL)
                print(str(e))
                raise e
    # end if else
    return data


def getRecordMetaData(recordId):
    ORG_URL = settings.AWG_IDR_SERVICE_URL + '/cve-id/' + str(recordId)
    org_params = {}

    # try/except block to catch integrity error in case the ID doesn't exist
    try:
        # Attempt to get org from RSUS
        record_result = call_idr_service('get', BASE_HEADERS, ORG_URL, org_params)
        data = json.loads(record_result)
        if "owning_cna" in data:
            return data
        else:
            raise Exception(str(recordId) + " did not find an owning_cna.")
    except Exception as e:
        print(str(e))
        raise e
    return None
    

def getOrgData():
    global all_orgs

    ORG_URL = settings.AWG_IDR_SERVICE_URL + '/org'
    org_params = {}

    # try/except block to catch integrity error in case the org doesn't exist
    try:
        # Attempt to get org from RSUS
        org_result = call_idr_service('get', BASE_HEADERS, ORG_URL, org_params)
        data = json.loads(org_result)
        for org in data["organizations"]:
            all_orgs[org["UUID"]] = org
        # all_orgs[orgId] = data
    except Exception as e:
        print(str(e))
        raise e
    return True

def getRequesterMap():
    global requester_map
    
    if len(requester_map) < 1 :
        with open('user_map.csv', newline='') as csvfile:
            req_reader = csv.reader(csvfile, delimiter=',')
            for row in req_reader:
                requester_map[row[0]] = row

    return True            


def getReferenceTagMap():
    global reference_tag_map
    
    if len(reference_tag_map) < 1 :
        with open("ref_tag_map.json") as ref_tag_file:
            reference_tag_map = json.load(ref_tag_file)
    return True            


def getV5ReferenceTagValue(v4Tag):
    global reference_tag_map
    v5Tags = None
    v4Test = v4Tag.casefold()
    refhit = False
    for tagMap in reference_tag_map["referenceMaps"]:
        if v4Test == tagMap["v4"].casefold():
                v5Tags = tagMap["v5"]
                refhit = True
                break
    if not refhit:
        # print("Missed Ref Tag: " + v4Tag)
        pass
        
    return v5Tags


def call_idr_service(action, req_header, IDR_URL, params=None, content=None):
    """
    :param action: GET, POST, ...
    :param req_header: JSON object formated for IDR service endpoint
    :param IDR_URL: string value of URL for IDR Service endpoint
    :param params: querystring paramater dictionary
    :param content: call body
    :return: response received from IDR

    :raises: Integrity Error, includes list of errors encountered, CPS may be
    out of sync with IDR at this point, need to trigger or wait for sync
    """
    IDR_Timeout = settings.AWG_SERVICE_TIMEOUT
    IDR_Response_Received = False
    if action:
        try:
            if action.lower() == 'post':
                IDR_Response = requests.post(
                    IDR_URL,
                    params=params,
                    headers=req_header,
                    json=content,
                    timeout=IDR_Timeout,
                    cert=None)
            elif action.lower() == 'put':
                IDR_Response = requests.put(
                    IDR_URL,
                    params=params,
                    headers=req_header,
                    json=content,
                    timeout=IDR_Timeout,
                    cert=None)
            elif action.lower() == 'get':
                IDR_Response = requests.get(
                    IDR_URL,
                    params=params,
                    headers=req_header,
                    json=content,
                    timeout=IDR_Timeout,
                    cert=None)
            else:
                raise Exception("HTTP action not expected.")

            IDR_Response_Received = True
        except requests.exceptions.ConnectTimeout:
            IDR_Error = f"Connection timeout to: {IDR_URL}"
        except requests.exceptions.Timeout:
            IDR_Error = "Request timeout from IDR request."
        except requests.exceptions.ReadTimeout:
            IDR_Error = "Request timeout, no data from IDR request."
        except requests.exceptions.HTTPError:
            IDR_Error = "IDR HTTPError occurred."
        except requests.exceptions.ConnectionError:
            IDR_Error = "IDR ConnectionError occurred."
        except requests.exceptions.RequestException:
            IDR_Error = "IDR Request error occurred."

    if not IDR_Response_Received:
        raise Exception(f'IDR service access failure: {IDR_Error}')
    else:  # we received a response
        IDR_Status_Code = IDR_Response.status_code
        IDR_Body = IDR_Response.content.decode('utf-8')
        # status codes for success 200, 206
        # 200 = fully successful
        # 206 = partial success, example reserved 6 IDs out of 10 requested
        if IDR_Status_Code == 200 or IDR_Status_Code == 206:  # was our request OK?
            return IDR_Body
        else:
            err_msg = json.loads(IDR_Body)
            raise Exception("IDR Error: " + err_msg['message'])

def lang_code_3_from_2(lang_code):
    """
    :param: 2 letter language code to convert
    :return: 3 letter language code
    :raises:

    """
    if lang_code:
        return Language.get(lang_code).to_alpha3()
    else:
        raise Exception("No language code provided")


def lang_code_2_from_3(lang_code):
    """
    convert to BCP-47 standard
    :param: 3 letter language code to convert
    :return: 2 letter language code
    :raises:

    """
    if lang_code:
        return Language.get(lang_code).language
    else:
        raise Exception("No language code provided")
        
def convertLangInArray(sArray):
    na = []
    for aval in sArray:
        if "lang" in aval:
            # sArray[aval]["lang"] = lang_code_2_from_3(aval["lang"])
            aval["lang"] = lang_code_2_from_3(aval["lang"])
            na.append(aval)
        # end if "lang"
    # end if aval
    return na


def testCVEServicesConnection():
    result = True
    if IDR_Health_Check() != 200:
        result = False
    
    return result


def IDR_Health_Check():
    # if healthy expect 200 for a response
    IDR_Timeout = settings.AWG_SERVICE_TIMEOUT
    IDR_URL = settings.AWG_IDR_SERVICE_URL + settings.AWG_IDR_ENDPOINT_HEALTHCHECK
    IDR_Response = None
    try:
        IDR_Response = requests.get(
            IDR_URL,
            # NOTE: these are requests.post optional arguments
            timeout=IDR_Timeout)
    except Exception as e:
        print(f'IDR health check failed: {e}')
        pass

    if IDR_Response:
        print (str(IDR_Response.status_code))
        return IDR_Response.status_code
    else:  # requests failed, raise exception
        print(f'cps/shared/utils.py/IDR_Health_Check(): {IDR_Response}')
        # TODO: we aren't raising an exception here, and, we shouldn't return
        # a random integer?
        return 404

def hasVal(v):
    return (v != "" and v != {"lang":"en","value":""}
        and v != {"lang":"en"} and v != [] and v!= {} and v!= [{"lang": "en", "value": ""}]
        and v!= [{"lang": "en"}])

def clean_empty(d):
    if isinstance(d, dict):
        return {
            k: v
            for k, v in ((k, clean_empty(v)) for k, v in d.items())
            if hasVal(v)
        }
    if isinstance(d, list):
        x = [v for v in map(clean_empty, d) if hasVal(v)]
    return d

def reEncodeUrl(inRef):
    # use requote_uri to quote most chars, then urllib.parse.quote to encode any remaining unsafe chars
    return urllib.parse.quote(requote_uri(inRef), safe=':/=&?#%+')
    # outRef = urllib.parse.quote(outRef)

def buildImpactOther(key_str, content):
    o_impact = {}
    o_impact["type"] = "unknown"
    if isinstance(content, dict):
        o_impact["content"] = content.copy()
    elif isinstance(content, list):
        o_impact["content"] = content.copy()
    else:
        # wrap value in object
        o_impact["content"] = {key_str:content}
    return o_impact


def getRejectedDate(cveId, recordHistory):
    # newTime = time.perf_counter()

    global historyDateTimeFormat
    firstRejected = datetime.datetime.combine(datetime.date.today(), datetime.datetime.min.time())
    lastUpdated = firstRejected
    sawRejectedDate = False
    
    for h in recordHistory:
        hdt = datetime.datetime.strptime(h["history_date"],historyDateTimeFormat)
        if h["HType"] == "Rejected":
            firstRejected = min(datetime.datetime.strptime(hdt,historyDateTimeFormat), firstRejected)
            sawRejectedDate = True
        if h["HType"] == "Modified":
            lastUpdated = min(datetime.datetime.strptime(hdt,historyDateTimeFormat), lastUpdated)    
    if not sawRejectedDate:
        firstRejected = lastUpdated
        
    # setTime = time.perf_counter() - newTime
    # print("getRejectedDate took:"  + '{0:2f}'.format(setTime))
    return firstRejected


def getLastUpdated(cveId, recordHistory):
    # newTime = time.perf_counter()

    global historyDateTimeFormat
    
    lastUpdated = datetime.datetime.min
    if recordHistory:
        for h in recordHistory:
            if h["HType"] == "Modified" or h["HType"] == "Rejected":  
                lastUpdated = max(datetime.datetime.strptime(h["history_date"],historyDateTimeFormat), lastUpdated)
    else:
        lastUpdated = datetime.datetime.combine(datetime.date.today(), datetime.datetime.min.time())
    
    # setTime = time.perf_counter() - newTime
    # print("getLastUpdated took:"  + '{0:2f}'.format(setTime))
    return lastUpdated


def getDatePublished(cveId, recordHistory):
    # newTime = time.perf_counter()

    global historyDateTimeFormat
    
    pubDate = datetime.datetime.now()
    for h in recordHistory:
        if h["populated_date"] != "null":
            pubDate = min(datetime.datetime.strptime(h["populated_date"],historyDateTimeFormat), pubDate)
    
    # setTime = time.perf_counter() - newTime
    # print("getDatePublished took:"  + '{0:2f}'.format(setTime))
    return pubDate


def getReservedDate(cveId, recordHistory):
    # newTime = time.perf_counter()

    historyReservedDateFormat = '%Y-%m-%d'
    
    resDate = datetime.datetime.now()
    for h in recordHistory:
        if h["reserved_date"] != "null":
            resDate = min(datetime.datetime.strptime(h["reserved_date"],historyReservedDateFormat), resDate)
    
    # setTime = time.perf_counter() - newTime
    # print("getReservedDate took:"  + '{0:2f}'.format(setTime))
    return resDate


if __name__ == "__main__":
   main(sys.argv[1:])
