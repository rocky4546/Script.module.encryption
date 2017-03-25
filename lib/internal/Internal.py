import aes, pyDes, hashlib, os, binascii
import properties
import json, abc
import platform_
import xbmcutil, errors

def LOG(msg):
	print 'script.module.password.storage: ' + msg
	
try:
	from getpass import getpass, lazy_getpass, saveKeyringPass, showMessage, clearKeyMemory, getRandomKey
except ImportError:
	#For testing
	from getpass import getpass
	lazy_getpass = getpass
	saveKeyringPass = lambda x: len(x)
	showMessage = LOG
	def clearKeyMemory(): pass
	def getRandomKey(): return 'RANDOM_KEY_TEST'

T = xbmcutil.ADDON.getLocalizedString

def add_metaclass(metaclass):
	"""Class decorator for creating a class with a metaclass."""
	def wrapper(cls):
		orig_vars = cls.__dict__.copy()
		orig_vars.pop('__dict__', None)
		orig_vars.pop('__weakref__', None)
		for slots_var in orig_vars.get('__slots__', ()):
			orig_vars.pop(slots_var)
		return metaclass(cls.__name__, cls.__bases__, orig_vars)
	return wrapper

class KeyringBackendMeta(abc.ABCMeta):
	"""
	A metaclass that's both an ABCMeta and a type that keeps a registry of
	all (non-abstract) types.
	"""
	def __init__(cls, name, bases, dict_):  # @NoSelf
		super(KeyringBackendMeta, cls).__init__(name, bases, dict_)
		if not hasattr(cls, '_classes'):
			cls._classes = set()
		classes = cls._classes
		if not cls.__abstractmethods__:
			classes.add(cls)

@add_metaclass(KeyringBackendMeta)
class KeyringBackend(object):
	"""The abstract base class of the keyring, every backend must implement
	this interface.
	"""

	#@abc.abstractproperty
	def priority(cls):  # @NoSelf
		"""
		Each backend class must supply a priority, a number (float or integer)
		indicating the priority of the backend relative to all other backends.
		The priority need not be static -- it may (and should) vary based
		attributes of the environment in which is runs (platform, available
		packages, etc.).

		A higher number indicates a higher priority. The priority should raise
		a RuntimeError with a message indicating the underlying cause if the
		backend is not suitable for the current environment.

		As a rule of thumb, a priority between zero but less than one is
		suitable, but a priority of one or greater is recommended.
		"""

	@properties.ClassProperty
	@classmethod
	def viable(cls):
		with errors.ExceptionRaisedContext() as exc:
			cls.priority
		return not bool(exc)

	@abc.abstractmethod
	def get_password(self, service, username):
		"""Get password of the username for the service
		"""
		return None

	@abc.abstractmethod
	def set_password(self, service, username, password):
		"""Set password for the username of the service
		"""
		raise errors.PasswordSetError("reason")

	# for backward-compatibility, don't require a backend to implement
	#  delete_password
	#@abc.abstractmethod
	def delete_password(self, service, username):
		"""Delete the password for the username of the service.
		"""
		raise errors.PasswordDeleteError("reason")

class BaseKeyring(KeyringBackend):
	"""
	BaseKeyring is a file-based implementation of keyring.

	This keyring stores the password directly in the file and provides methods
	which may be overridden by subclasses to support
	encryption and decryption. The encrypted payload is stored in base64
	format.
	"""

	@properties.NonDataProperty
	def file_path(self):
		"""
		The path to the file where passwords are stored. This property
		may be overridden by the subclass or at the instance level.
		"""
		return os.path.join(platform_.data_root(), self.filename)

	@abc.abstractproperty
	def filename(self):
		"""
		The filename used to store the passwords.
		"""

	@abc.abstractmethod
	def encrypt(self, password):
		"""
		Given a password (byte string), return an encrypted byte string.
		"""

	@abc.abstractmethod
	def decrypt(self, password_encrypted):
		"""
		Given a password encrypted by a previous call to `encrypt`, return
		the original byte string.
		"""

	def get_password(self, service, username):
		"""
		Read the password from the file.
		"""

	def set_password(self, service, username, password):
		"""Write the password in the file.
		"""

	def _ensure_file_path(self):
		"""
		Ensure the storage path exists.
		If it doesn't, create it with "go-rwx" permissions.
		"""
		storage_root = os.path.dirname(self.file_path)
		if storage_root and not os.path.isdir(storage_root):
			os.makedirs(storage_root)
		if not os.path.isfile(self.file_path):
			# create the file without group/world permissions
			with open(self.file_path, 'w'):
				pass
			user_read_write = 0o600
			try:
				os.chmod(self.file_path, user_read_write)
			except OSError:
				LOG('Could not chmod storage file')

	def delete_password(self, service, username):
		"""Delete the password for the username of the service.
		"""

class PythonEncryptedKeyring(BaseKeyring):
	_check = 'password reference value'
	_encryption_version = 0
	filename = 'python_crypted_pass.json'
	
	@properties.ClassProperty
	@classmethod
	def priority(self):
		return .6
	
	@properties.NonDataProperty
	def keyring_key(self):
		# _unlock or _init_file will set the key or raise an exception
		if self._check_file():
			self._unlock()
		else:
			self._init_file()
		return self.keyring_key

	def reset(self):
		if os.path.exists(self.file_path): os.remove(self.file_path)
		
	def _init_file(self,keyring_key=None):
		"""
		Initialize a new password file and set the reference password.
		"""
		self.keyring_key = keyring_key or self._get_new_password() or ''
		saveKeyringPass(self.keyring_key)
		#We create and encrypt and store a secondary key, so the primary key can be changed and all we need to do is decrypt and restore the secondary
		secondary_key = getRandomKey()
		passwords_dict = {	'key':self.encrypt(self.keyring_key, secondary_key),
							'check':self.encrypt(self.keyring_key, self._check),
							'version':self._encryption_version,
							'storage':{}
		}
		self._write_passwords(passwords_dict)
	
	def _check_file(self):
		"""
		Check if the file exists and has the expected password reference.
		"""
		if not os.path.exists(self.file_path): return False
		passwords_dict = self._read_passwords()
		return 'check' in passwords_dict
	
	def _check_reference(self,passwords_dict):
		try:
			check = self.decrypt(self.keyring_key, passwords_dict['check'])
			assert check == self._check
		except (ValueError,AssertionError):
			self._lock()
			return False
		return True
	
	def _unlock(self,key=None):
		"""
		Unlock this keyring by getting the password for the keyring from the
		user.
		"""
		key = key or lazy_getpass(T(32025))
		if key == None: raise errors.AbortException('Abort at unlock')
		self.keyring_key = key
		passwords_dict = self._read_passwords()
		if not self._check_reference(passwords_dict):
			clearKeyMemory()
			raise errors.IncorrectKeyringKeyException()

	def _lock(self):
		"""
		Remove the keyring key from this instance.
		"""
		del self.keyring_key
		
	def _get_secondary_key(self,passwords_dict):
		return self.decrypt(self.keyring_key, passwords_dict['key'])
		
	def change_keyring_password(self,keyring_key=None):
		passwords_dict = self._read_passwords()
		secondary_key = self._get_secondary_key(passwords_dict)

		if keyring_key:
			self.keyring_key = keyring_key
		else:
			key = self._get_new_password()
			if not key: return
			self.keyring_key = key
		passwords_dict['key'] = self.encrypt(self.keyring_key, secondary_key)
		passwords_dict['check'] = self.encrypt(self.keyring_key, self._check)
		self._write_passwords(passwords_dict)
		return self.keyring_key
		
	def get_password(self, service, username):
		"""
		Read the password from the file.
		"""
		try:
			passwords_dict = self._read_passwords()
		except ValueError: #ValueError if empty json
			return None
			
		key = self._get_secondary_key(passwords_dict)
		try:
			return self.decrypt(key, passwords_dict['storage'][service][username])
		except KeyError: #KeyError if password not set
			return None
		

	def set_password(self, service, username, password):
		"""Write the password in the file.
		"""
		passwords_dict = self._read_passwords()
		key = self._get_secondary_key(passwords_dict)
		if not service in passwords_dict['storage']: passwords_dict['storage'][service] = {}
		encrypted_password = self.encrypt(key, password)
		passwords_dict['storage'][service][username] = encrypted_password
		self._write_passwords(passwords_dict)
		

	def delete_password(self, service, username):
		"""Delete the password for the username of the service.
		"""
		passwords_dict = self._read_passwords()
		try:
			del passwords_dict['storage'][service][username]
		except KeyError:
			raise errors.PasswordDeleteError("Password not found")
		self._write_passwords(passwords_dict)
			
	def _get_new_password(self):
		while True:
			password = getpass(T(32026))
			if password == None: raise errors.AbortException()
			confirm = getpass(T(32027),confirm=True)
			if confirm == None: raise errors.AbortException()
			if password != confirm:
				showMessage(T(32028))
				continue
			if '' == password.strip():
				# forbid the blank password
				showMessage(T(32029))
				continue
			return password
		
	def _read_passwords(self):
		if not os.path.exists(self.file_path): self._init_file()
		with open(self.file_path,'r') as pass_file:
			return json.load(pass_file)
		
	
	def _write_passwords(self,passwords_dict):
		self._ensure_file_path()
		with open(self.file_path,'w') as pass_file:
			json.dump(passwords_dict,pass_file,separators=(',',':'),sort_keys=True,indent=4)
			
	def encrypt(self, key, password):
		return encrypt(key, password)
		
	def decrypt(self, key, password_encrypted):
		return decrypt(key, password_encrypted)
		
def encrypt(key, password):
	password = _encryptDes(key,password)
	return binascii.hexlify(aes.encryptData(hashlib.md5(key).digest(),password))

def decrypt(key, password_encrypted):
	password = aes.decryptData(hashlib.md5(key).digest(),binascii.unhexlify(password_encrypted))
	return _decryptDes(key,password)

def _encryptDes(key,password):
	key = hashlib.sha224(key).digest()[:24]
	des = pyDes.triple_des(key)
	return des.encrypt(password,padmode=pyDes.PAD_PKCS5)
	
def _decryptDes(key,password):
	key = hashlib.sha224(key).digest()[:24]
	des = pyDes.triple_des(key)
	return des.decrypt(password,padmode=pyDes.PAD_PKCS5)
