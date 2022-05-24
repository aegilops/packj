from __future__ import print_function
import sys
import os
import json
import logging

from util.net import __parse_url, download_file, check_site_exist, check_domain_popular
from util.dates import datetime_delta
from util.email_validity import check_email_address
from util.files import write_json_to_file, read_from_csv
from util.enum_util import PackageManagerEnum, LanguageEnum, DistanceAlgorithmEnum, TraceTypeEnum, DataTypeEnum
from util.formatting import human_format

from parse_apis import parse_api_usage
from pm_util import get_pm_proxy
from static_util import get_static_proxy_for_language

# sys.version_info[0] is the major version number. sys.version_info[1] is minor
if sys.version_info[0] != 3:
	print("\n*** WARNING *** Please use Python 3! Exiting.")
	exit(1)

def get_threat_model(filename='threats.csv'):
	threat_model = {}
	for line in read_from_csv(filename, skip_header=True):
		typ = line[0]
		attr = line[1].strip('\n')
		threat_model[attr] = typ
	return threat_model

def alert_user(alert_type, threat_model, reason, risks):
	if alert_type in threat_model:
		risk_cat = threat_model[alert_type]
		if risk_cat not in risks:
			risks[risk_cat] = []
		risks[risk_cat].append('%s: %s' % (alert_type, reason))
	return risks

def analyze_release_history(pkg_name, ver_str, pkg_info=None, risks={}, report={}):
	try:
		print("[+] Checking release history...", end='', flush=True)

		# get package release history
		release_history = pm_proxy.get_release_history(pkg_name, pkg_info=pkg_info)
		assert release_history, "no data!"

		if len(release_history) < 2:
			reason = 'only %s versions released' % (len(release_history))
			alert_type = 'few versions or releases'
			risks = alert_user(alert_type, threat_model, reason, risks)

		else:
			try:
				days = release_history[ver_str]['days_since_last_release']

				# check if the latest release is made after a long gap (indicative of package takeover)
				if days and days > 180:
					reason = 'version released after %d days' % (days)
					alert_type = 'version release after a long gap'
					risks = alert_user(alert_type, threat_model, reason, risks)
			except Exception as ee:
				print(str(ee))
				pass

		print("OK [%d versions]" % (len(release_history)))
		report['releases'] = release_history
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_version(ver_info, risks={}, report={}):
	try:
		print("[+] Checking version...", end='', flush=True)

		assert ver_info, "no data!"

		# check upload timestamp
		try:
			uploaded = ver_info['uploaded']
			days = datetime_delta(uploaded, days=True)
		except KeyError:
			raise Exception('parse error')

		# check if the latest release is too old (unmaintained package)
		if not uploaded or days > 365:
			reason = 'no release date' if not uploaded else '%d days old' % (days)
			alert_type = 'old package'
			risks = alert_user(alert_type, threat_model, reason, risks)

		print("OK [%d days old]" % (days))
		report["version"] = ver_info
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_cves(pm_name, pkg_name, ver_str, risks={}, report={}):
	try:
		print("[+] Checking for CVEs...", end='', flush=True)
		from osv import get_pkgver_vulns
		vuln_list = get_pkgver_vulns(pm_name, pkg_name, ver_str)
		if vuln_list:
			alert_type = 'contains known vulnerablities (CVEs)'
			reason = 'contains %s' % (','.join(vul['id'] for vul in vuln_list))
			risks = alert_user(alert_type, threat_model, reason, risks)
		else:
			vuln_list = []
		print("OK [%s found]" % (len(vuln_list)))
		report["vulnerabilities"] = vuln_list
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_downloads(pm_proxy, pkg_name, ver_str=None, pkg_info=None, risks={}, report={}):
	try:
		print("[+] Checking downloads...", end='', flush=True)
		ret = pm_proxy.get_downloads(pkg_name)
		if ret < 1000:
			reason = 'only %d weekly downloads' % (ret)
			alert_type = 'few downloads'
			risks = alert_user(alert_type, threat_model, reason, risks)
		print("OK [%s weekly]" % (human_format(ret)))
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_homepage(pkg_name, ver_str=None, pkg_info=None, risks={}, report={}):
	try:
		print("[+] Checking homepage...", end='', flush=True)
		url = pm_proxy.get_homepage(pkg_name, ver_str=ver_str, pkg_info=pkg_info)
		if not url:
			reason = 'no homepage'
			alert_type = 'invalid or no homepage'
			risks = alert_user(alert_type, threat_model, reason, risks)

		# check if insecure
		ret = __parse_url(url)
		if ret.scheme != 'https':
			reason = 'insecure webpage'
			alert_type = 'invalid or no homepage'
			risks = alert_user(alert_type, threat_model, reason, risks)

		# check if an existent webpage
		valid_site, reason = check_site_exist(url)
		if not valid_site:
			alert_type = 'invalid or no homepage'
			risks = alert_user(alert_type, threat_model, reason, risks)

		# check if a popular webpage
		elif check_domain_popular(url):
			reason = 'invalid (popular) webpage'
			alert_type = 'invalid or no homepage'
			risks = alert_user(alert_type, threat_model, reason, risks)
		print("OK [%s]" % (url))
		report["homepage"] = url
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_repo(pkg_name, ver_str=None, pkg_info=None, ver_info=None, risks={}, report={}):
	try:
		print("[+] Checking repo...", end='', flush=True)
		popular_hosting_services = ['https://github.com/','https://gitlab.com/','git+https://github.com/','git://github.com/']
		repo = pm_proxy.get_repo(pkg_name, ver_str=ver_str, pkg_info=pkg_info, ver_info=ver_info)
		if not repo:
			repo = pm_proxy.get_homepage(pkg_name, ver_str=ver_str, pkg_info=pkg_info)
			if not repo or not repo.startswith(tuple(popular_hosting_services)):
				repo = None
		if not repo:
			repo = pm_proxy.get_download_url(pkg_name, ver_str=ver_str, pkg_info=pkg_info)
			if not repo or not repo.startswith(tuple(popular_hosting_services)):
				repo = None
		if not repo:
			reason = 'no source repo found'
			alert_type = 'invalid or no source repo'
			risks = alert_user(alert_type, threat_model, reason, risks)
		elif not repo.startswith(tuple(popular_hosting_services)):
			reason = 'invalid source repo %s' % (repo)
			alert_type = 'invalid or no source repo'
			risks = alert_user(alert_type, threat_model, reason, risks)
		elif repo.strip('/') in ['https://github.com/pypa/sampleproject', 'https://github.com/kubernetes/kubernetes']:
			reason = 'invalid source repo %s' % (repo)
			alert_type = 'invalid or no source repo'
			risks = alert_user(alert_type, threat_model, reason, risks)
		print("OK [%s]" % (repo))
		report["repo"] = repo
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_readme(pkg_name, ver_str=None, pkg_info=None, risks={}, report={}):
	try:
		print("[+] Checking readme...", end='', flush=True)
		descr = pm_proxy.get_description(pkg_name, ver_str=ver_str, pkg_info=pkg_info)
		if not descr or len(descr) < 100:
			reason = 'no description' if not descr else 'insufficient description'
			alert_type = 'no or insufficient readme'
			risks = alert_user(alert_type, threat_model, reason, risks)
		print("OK [%d bytes]" % (len(descr)))
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_author(pkg_name, ver_str=None, pkg_info=None, ver_info=None, risks={}, report={}):
	try:
		print("[+] Checking author...", end='', flush=True)

		author_info = pm_proxy.get_author(pkg_name, ver_str=ver_str, pkg_info=pkg_info, ver_info=ver_info)
		assert author_info, "no data!"

		try:
			email = author_info['email']
		except KeyError:
			email = None

		# check author email
		valid, valid_with_dns = check_email_address(email)
		if not valid or not valid_with_dns:
			alert_type = 'invalid or no author email (2FA not enabled)'
			reason = 'no email' if not email else 'invalid author email' if not valid else 'expired author email domain'
			risks = alert_user(alert_type, threat_model, reason, risks)

		print("OK [%s]" % (email))
		report["author"] = author_info
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

def analyze_apis(pm_name, pkg_name, ver_str, filepath, risks={}, report={}):
	try:
		print("[+] Analyzing APIs...", end='', flush=True)
		if pm_name == 'pypi':
			language=LanguageEnum.python
			configpath = os.path.join('config','astgen_python_smt.config')
		elif pm_name == 'npm':
			language=LanguageEnum.javascript
			configpath = os.path.join('config','astgen_javascript_smt.config')
		else:
			raise Exception("Package manager %s not supported!")

		static = get_static_proxy_for_language(language=language)
		try:
			static.astgen(inpath=filepath, outfile=filepath+'.out', root=None, configpath=configpath,
				pkg_name=pkg_name, pkg_version=ver_str, evaluate_smt=False)
		except Exception as ee:
			logging.debug("Failed to parse: %s" % (str(ee)))
			raise Exception("parse error")

		assert os.path.exists(filepath+'.out'), "parse error!"

		perms = parse_api_usage(pm_name, filepath+'.out')
		assert perms, "No APIs found!"

		report_data = {}
		for p, usage in perms.items():
			if p == "SOURCE_FILE":
				alert_type = 'accesses files and dirs'
				reason = 'reads files and dirs'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SINK_FILE":
				alert_type = 'accesses files and dirs'
				reason = 'writes to files and dirs'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SINK_NETWORK":
				alert_type = 'communicates with external network'
				reason = 'sends data over the network %s'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SOURCE_NETWORK":
				alert_type = 'communicates with external network'
				reason = 'fetches data over the network'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p in "SINK_CODE_GENERATION":
				alert_type = 'generates new code at runtime'
				reason = 'generates new code at runtime'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif "SINK_PROCESS_OPERATION":
				alert_type = 'generates new code at runtime'
				reason = 'spawns new processes in background'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SOURCE_OBFUSCATION":
				alert_type = 'accesses obfuscated (hidden) code'
				reason = 'reads hidden code'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SOURCE_SETTINGS":
				alert_type = 'accesses system/environment variables'
				reason = 'reads system settings or environment variables'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SINK_UNCLASSIFIED":
				alert_type = 'changes system/environment variables'
				reason = 'modifies system settings or environment variables'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SOURCE_ACCOUNT":
				alert_type = 'changes system/environment variables'
				reason = 'modifies system settings or environment variables'
				risks = alert_user(alert_type, threat_model, reason, risks)
			elif p == "SOURCE_USER_INPUT":
				alert_type = 'reads user input'
				reason = 'reads user input'
				risks = alert_user(alert_type, threat_model, reason, risks)

			# report
			if reason not in report_data:
				report_data[reason] = usage
			else:
				report_data[reason] += usage

		print("OK [%d analyzed]" % (len(perms)))
		report["permissions"] = report_data
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
	finally:
		return risks, report

if __name__ == "__main__":
	from static_util import astgen

	threat_model = get_threat_model()

	pm_name = sys.argv[1].lower()
	if pm_name == 'pypi':
		pm = PackageManagerEnum.pypi
	elif pm_name == 'npm':
		pm = PackageManagerEnum.npmjs
	else:
		print("Package manager %s is not supported" % (pm_name))
		exit(1)

	pm_proxy = get_pm_proxy(pm, cache_dir=None, isolate_pkg_info=False)
	assert pm_proxy, "%s not supported" % (pm_name)

	ver_str = None
	pkg_name = sys.argv[2]
	if '==' in pkg_name:
		pkg_name, ver_str = pkg_name.split('==')

	# get version metadata
	try:
		print("[+] Fetching '%s' from %s..." % (pkg_name, pm_name), end='', flush=True)
		pkg_info = pm_proxy.get_metadata(pkg_name=pkg_name, pkg_version=ver_str)
		assert pkg_info, "package not found!"

		try:
			pkg_name = pkg_info['info']['name']
		except KeyError:
			pass

		ver_info = pm_proxy.get_version(pkg_name, ver_str=ver_str, pkg_info=pkg_info)
		assert ver_info, "No version info!"

		#print(json.dumps(ver_info, indent=4))
		if not ver_str:
			ver_str = ver_info['tag']

		print("OK [ver %s]" % (ver_str))
	except Exception as e:
		print("FAILED [%s]" % (str(e)))
		exit(1)

	risks = {}
	report = {}

	# analyze metadata
	risks, report = analyze_version(ver_info, risks=risks, report=report)
	risks, report = analyze_release_history(pkg_name, ver_str, pkg_info=pkg_info, risks=risks, report=report)
	risks, report = analyze_author(pkg_name, ver_str=ver_str, pkg_info=pkg_info, ver_info=ver_info, risks=risks, report=report)
	risks, report = analyze_readme(pkg_name, ver_str=ver_str, pkg_info=pkg_info, risks=risks, report=report)
	risks, report = analyze_homepage(pkg_name, ver_str=ver_str, pkg_info=pkg_info, risks=risks, report=report)
	risks, report = analyze_downloads(pm_proxy, pkg_name, ver_str=ver_str, pkg_info=pkg_info, risks=risks, report=report)
	risks, report = analyze_repo(pkg_name, ver_str=ver_str, pkg_info=pkg_info, ver_info=ver_info, risks=risks, report=report)
	risks, report = analyze_cves(pm_name, pkg_name, ver_str=ver_str, risks=risks, report=report)

	# download package
	try:
		print("[+] Downloading package '%s' (ver %s) from %s..." % (pkg_name, ver_str, pm_name), end='', flush=True)
		filepath, size = download_file(ver_info['url'])
		print("OK [%0.2f KB]" % (float(size)/1024))
	except KeyError:
		print("FAILED [URL missing]")
	except Exception as e:
		print("FAILED [%s]" % (str(e)))

	if filepath:
		risks, report = analyze_apis(pm_name, pkg_name, ver_str, filepath, risks=risks, report=report)

	print("=============================================")
	if not len(risks):
		print("[+] No risks found!")
		report["risks"] = None
	else:
		print("[+] %d risk(s) found, package is %s!" % (sum(len(v) for v in risks.values()), ', '.join(risks.keys())))
		report["risks"] = risks
	filename = "%s-%s-%s.json" % (pm_name, pkg_name, ver_str)
	write_json_to_file(filename, report, indent=4)
	print("=> Complete report: %s" % (filename))

	if pm_name.lower() == 'pypi':
		print("=> View pre-vetted package report at https://packj.dev/package/PyPi/%s/%s" % (pkg_name, ver_str))
